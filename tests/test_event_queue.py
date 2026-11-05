"""事件队列(先应答后处理)测试。

内存模式:异步消费且保序、失败退避重试后成功、重试耗尽写死信、
失败不阻塞后续事件、队列满返回 False、关停排空在途事件、
H6 修复后退避重试在队列持续繁忙时不被饿死。

outbox 持久化模式:submit 先落库、消费后标 done、重复 key 幂等去重、
失败退避重试并最终 done、worker「重启」(新队列实例)后恢复处理未完成事件。
"""
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.event_queue import EventQueue


def _wait(cond, timeout: float = 8.0) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.01)
    return False


def _make_event() -> "P2ImMessageReceiveV1":
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

    return P2ImMessageReceiveV1(
        {
            "event": {
                "sender": {"sender_id": {"open_id": "ou_q1"}, "sender_type": "user"},
                "message": {
                    "message_id": "om_q1",
                    "chat_id": "oc_q",
                    "message_type": "text",
                    "create_time": 1700000000000,
                    "content": '{"text":"hello queue"}',
                    "parent_id": "",
                    "root_id": "",
                    "mentions": [],
                },
            }
        }
    )


def _make_session_factory():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.models import Base, EventOutbox

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session, EventOutbox


# ============================================================ 内存模式
def test_fifo_consume():
    got: list = []
    q = EventQueue(process_fn=lambda x: got.append(x), retry_delays=(0.01,))
    q.start()
    for i in range(5):
        assert q.submit(i)
    assert _wait(lambda: len(got) == 5)
    q.stop()
    assert got == [0, 1, 2, 3, 4]


def test_retry_then_success(tmp_path):
    calls = {"n": 0}
    got: list = []

    def fn(x):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("boom")
        got.append(x)

    dl = str(tmp_path / "d.jsonl")
    q = EventQueue(process_fn=fn, retry_delays=(0.01, 0.01), dead_letter=dl)
    q.start()
    q.submit("a")
    assert _wait(lambda: got == ["a"])
    q.stop()
    assert calls["n"] == 3
    assert not Path(dl).exists()


def test_dead_letter_after_retries(tmp_path):
    dl = tmp_path / "d.jsonl"

    def fn(x):
        raise RuntimeError("always down")

    q = EventQueue(process_fn=fn, retry_delays=(0.01, 0.01), dead_letter=str(dl))
    q.start()
    q.submit({"mid": "m1"})
    assert _wait(lambda: dl.exists() and dl.read_text(encoding="utf-8").strip())
    q.stop()
    line = json.loads(dl.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert line["event"]["mid"] == "m1"
    assert line["error"] == "重试耗尽"


def test_failure_does_not_block_queue(tmp_path):
    got: set = set()

    def fn(x):
        if x == "bad":
            raise RuntimeError("down")
        got.add(x)

    q = EventQueue(process_fn=fn, retry_delays=(0.05, 0.05), dead_letter=str(tmp_path / "d.jsonl"))
    q.start()
    q.submit("bad")
    q.submit("good1")
    q.submit("good2")
    assert _wait(lambda: {"good1", "good2"} <= got)
    q.stop()
    assert "bad" not in got


def test_submit_full_returns_false():
    started = threading.Event()
    release = threading.Event()

    def fn(x):
        started.set()
        release.wait(timeout=5)

    q = EventQueue(process_fn=fn, maxsize=1)
    q.start()
    assert q.submit("blocker")  # 消费者正卡在它上面
    assert _wait(lambda: started.is_set())
    assert q.submit("filler")  # 占满唯一的空位
    assert not q.submit("overflow")
    release.set()
    q.stop()


def test_stop_drains_pending():
    got: list = []
    q = EventQueue(process_fn=lambda x: got.append(x))
    q.start()
    for i in range(10):
        q.submit(i)
    q.stop(drain=True)
    assert sorted(got) == list(range(10))


def test_queue_drives_message_pipeline():
    """集成(内存模式):队列消费的事件走正常入库管道,落库成功。"""
    Session, _ = _make_session_factory()

    from app.services.message_service import MessageService

    def fn(data):
        with Session() as s:
            MessageService(s, allowed_chat_ids=set()).handle_receive_event(data)

    q = EventQueue(process_fn=fn, retry_delays=(0.01,))
    q.start()
    q.submit(_make_event())
    assert _wait(lambda: _count_messages(Session) == 1)
    with Session() as s:
        from app.models import Message

        row = s.query(Message).one()
        assert row.text == "hello queue"
    q.stop()


def _count_messages(Session) -> int:
    from app.models import Message

    with Session() as s:
        return s.query(Message).count()


# ============================================================ outbox 持久化模式
def _outbox_rows(Session):
    from app.models import EventOutbox

    with Session() as s:
        return s.query(EventOutbox).order_by(EventOutbox.id).all()


def test_persistent_submit_then_done():
    Session, _ = _make_session_factory()
    got: list = []

    def fn(data):
        got.append(data)
        with Session() as s:
            from app.models import Message

            s.add(Message(message_id="om_q1", chat_id="oc_q", text="hi",
                          create_time=1700000000000))
            s.commit()

    q = EventQueue(process_fn=fn, retry_delays=(0.01,), session_factory=Session)
    q.start()
    assert q.submit(_make_event())
    assert _wait(lambda: _outbox_rows(Session) and _outbox_rows(Session)[0].status == "done")
    q.stop()
    rows = _outbox_rows(Session)
    assert len(rows) == 1
    assert rows[0].status == "done"


def test_persistent_duplicate_key_dedup():
    Session, _ = _make_session_factory()
    q = EventQueue(process_fn=lambda x: None, session_factory=Session)
    # 同一 message_id → 同一 (chat_id, message_id) 去重键
    ev = _make_event()
    assert q.submit(ev)
    assert q.submit(ev)  # 幂等:唯一键冲突被吞并,仍返回 True
    rows = _outbox_rows(Session)
    assert len(rows) == 1


def test_persistent_retry_then_success():
    Session, _ = _make_session_factory()
    state = {"n": 0}

    def fn(data):
        if state["n"] < 2:
            state["n"] += 1
            raise RuntimeError("down")
        state["n"] += 1

    q = EventQueue(process_fn=fn, retry_delays=(0.01, 0.05), session_factory=Session)
    q.start()
    q.submit(_make_event())
    assert _wait(lambda: _outbox_rows(Session) and _outbox_rows(Session)[0].status == "done")
    q.stop()
    row = _outbox_rows(Session)[0]
    assert row.status == "done"
    assert row.attempt == 2  # 失败 2 次(retrying 2 次)后第 3 次成功


def test_persistent_survives_restart():
    """worker「重启」恢复:事件先落库,即使消费者从未运行/中断,
    新队列实例启动后仍能从 outbox 取出并处理,不丢失。"""
    Session, _ = _make_session_factory()

    # 第一个实例只负责落库(模拟回调落库后未及处理即崩溃)
    q1 = EventQueue(process_fn=lambda x: None, session_factory=Session)
    assert q1.submit(_make_event())
    assert len(_outbox_rows(Session)) == 1
    q1.stop()  # 未 start,纯粹写入

    # 第二个实例接管消费,应恢复 pending 事件
    got: list = []
    q2 = EventQueue(process_fn=lambda x: got.append(x), session_factory=Session)
    q2.start()
    assert _wait(lambda: got)
    assert _wait(lambda: _outbox_rows(Session)[0].status == "done")
    q2.stop()
