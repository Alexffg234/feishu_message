"""H5 媒体资源异步解析 worker 测试。

背景:事件队列消费者串行落库,若入库前同步下载上传媒体会长时间阻塞整个队列
(队头阻塞)。H5 把媒体解析移出消费线程——消息先快速落库(URL 留空),媒体
下载/上传交给独立 MediaWorker 并发完成并回填 URL。

本文件验证:
1. 消息落库后由 worker 异步并发回填 URL(不阻塞消费线程)。
2. 慢媒体(下载上传耗时长)不阻塞 submit 入队与消息落库。
3. worker 未启动 → 提交失败但 URL 留空、不异常、不中断消息落库。
4. 解析失败(异常)→ URL 留空、worker 不崩溃,后续任务不受影响。
5. 全链路:image 事件经 handle_receive_event 落库后,worker 回填 URL。

测试隔离:用「每连接独立」的文件式 SQLite + NullPool(生产形态),消费 worker
线程写、断言线程读各用独立连接,避免共享单连接上的跨线程竞态。解析用离线
monkeypatch 伪装,不触网。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.models import Base, Message
from app.services.media_worker import MediaWorker


def _make_env(tmp_path, name: str = "media_worker.db"):
    """建文件式 SQLite(每连接独立连接)并建全部表。返回 (engine, Session)。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / name}",
        connect_args={"check_same_thread": False, "timeout": 30},
        poolclass=NullPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return engine, Session


def _save_image(
    Session,
    message_id: str = "om_media",
    chat_id: str = "oc_media",
    image_key: str = "k1",
) -> None:
    """经 MessageService 落一条 image 消息(raw 含 image_key,URL 留空)。"""
    from app.schemas.message import MessageCreate
    from app.services.message_service import MessageService

    with Session() as s:
        svc = MessageService(s, allowed_chat_ids=set())
        create = MessageCreate(
            message_id=message_id,
            chat_id=chat_id,
            sender_id="ou_1",
            sender_type="user",
            message_type="image",
            text="",
            create_time="1700000000000",
            parent_message_id="",
            root_message_id="",
            raw={"image_key": image_key},
        )
        assert svc.save_message(create) is True


def _make_image_event(
    message_id: str = "om_img1", chat_id: str = "oc_media", image_key: str = "k1"
):
    """构造 image 类型飞书事件(lark SDK 对象),供 handle_receive_event 消费。"""
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

    return P2ImMessageReceiveV1(
        {
            "event": {
                "sender": {"sender_id": {"open_id": "ou_1"}, "sender_type": "user"},
                "message": {
                    "message_id": message_id,
                    "chat_id": chat_id,
                    "message_type": "image",
                    "create_time": 1700000000000,
                    "content": f'{{"image_key":"{image_key}"}}',
                    "parent_id": "",
                    "root_id": "",
                    "mentions": [],
                },
            }
        }
    )


def _get_raw(Session, message_id: str) -> dict:
    with Session() as s:
        row = s.query(Message).filter(Message.message_id == message_id).one()
        return dict(row.raw or {})


def test_worker_backfills_url_async(tmp_path, monkeypatch):
    """消息已落库(URL 留空)→ worker 独立 session 回填 URL。"""
    from app.services.media_worker import MediaWorker as _MW

    engine, Session = _make_env(tmp_path)
    _save_image(Session, message_id="om1", image_key="k1")

    # 离线伪解析:直接产出 URL,不触网
    def fake_resolve(raw, msg_type, message_id):
        raw["image_url"] = f"https://qiniu/fake/{raw.get('image_key')}"

    monkeypatch.setattr("app.services.message_service.resolve_media", fake_resolve)

    worker = MediaWorker(max_workers=2, session_factory=Session)
    worker.start()
    try:
        ok = worker.submit("oc_media", "om1", "image", {"image_key": "k1"})
        assert ok is True
        worker.shutdown(wait=True)
    finally:
        worker.shutdown(wait=True)
        engine.dispose()

    assert _get_raw(Session, "om1").get("image_url") == "https://qiniu/fake/k1"


def test_slow_media_does_not_block_submit(tmp_path, monkeypatch):
    """慢媒体占住 worker 线程时,submit 仍立即返回(入队非阻塞)。"""
    engine, Session = _make_env(tmp_path)

    worker = MediaWorker(max_workers=1, session_factory=Session)
    worker.start()

    in_handle = threading.Event()
    release = threading.Event()

    def slow_handle(job):
        in_handle.set()
        release.wait(3)

    monkeypatch.setattr(worker, "_handle", slow_handle)

    try:
        t0 = time.monotonic()
        ok1 = worker.submit("oc_media", "om_slow", "image", {"image_key": "k1"})
        assert ok1 is True
        assert time.monotonic() - t0 < 1.0  # 入队立即返回,不被慢任务阻塞
        assert in_handle.wait(2) is True  # handler 确实被慢任务占住

        # 第二个任务排在慢任务之后,但提交方依然立即返回(不排队等待)
        t1 = time.monotonic()
        ok2 = worker.submit("oc_media", "om_slow2", "image", {"image_key": "k2"})
        assert ok2 is True
        assert time.monotonic() - t1 < 1.0
    finally:
        release.set()
        worker.shutdown(wait=True)
        engine.dispose()


def test_worker_not_started_keeps_url_empty(tmp_path):
    """worker 未启动 → submit 返回 False,URL 留空,不异常。"""
    engine, Session = _make_env(tmp_path)
    _save_image(Session, message_id="om_nostart", image_key="k1")

    worker = MediaWorker(session_factory=Session)  # 故意不 start
    ok = worker.submit("oc_media", "om_nostart", "image", {"image_key": "k1"})
    assert ok is False
    worker.shutdown(wait=True)  # 幂等,不抛

    raw = _get_raw(Session, "om_nostart")
    assert raw.get("image_url") in (None, "")
    engine.dispose()


def test_resolve_error_keeps_url_empty(tmp_path, monkeypatch):
    """解析抛异常 → URL 留空,worker 不崩溃、不中断。"""
    engine, Session = _make_env(tmp_path)
    _save_image(Session, message_id="om_err", image_key="k1")

    def boom(raw, msg_type, message_id):
        raise RuntimeError("resolve failed")

    monkeypatch.setattr("app.services.message_service.resolve_media", boom)

    worker = MediaWorker(max_workers=2, session_factory=Session)
    worker.start()
    try:
        ok = worker.submit("oc_media", "om_err", "image", {"image_key": "k1"})
        assert ok is True
        worker.shutdown(wait=True)
    finally:
        worker.shutdown(wait=True)
        engine.dispose()

    raw = _get_raw(Session, "om_err")
    assert raw.get("image_url") in (None, "")


def test_handle_receive_event_enqueues_and_backfills(tmp_path, monkeypatch):
    """全链路:image 事件经 handle_receive_event 落库(URL 留空)→ worker 并发回填。"""
    from app.services.message_service import MessageService
    import app.services.media_worker as mw_mod

    engine, Session = _make_env(tmp_path)

    def fake_resolve(raw, msg_type, message_id):
        raw["image_url"] = f"https://qiniu/fake/{raw.get('image_key')}"

    monkeypatch.setattr("app.services.message_service.resolve_media", fake_resolve)

    worker = MediaWorker(max_workers=2, session_factory=Session)
    worker.start()
    # 让 message_service._enqueue_media 用我们的 worker(延迟 from...import 会取模块当前属性)
    monkeypatch.setattr(mw_mod, "get_media_worker", lambda: worker)
    try:
        with Session() as s:
            svc = MessageService(s, allowed_chat_ids=set())
            saved = svc.handle_receive_event(_make_image_event())
            assert saved is True  # 消息快速落库,不因媒体解析被阻塞
        # worker 并发回填 → URL 最终落库
        worker.shutdown(wait=True)
        assert _get_raw(Session, "om_img1").get("image_url") == "https://qiniu/fake/k1"
    finally:
        worker.shutdown(wait=True)
        engine.dispose()
