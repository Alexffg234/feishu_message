"""事件队列(先应答后处理)测试。

验证:异步消费且保序、失败退避重试后成功、重试耗尽写死信、
失败不阻塞后续事件、队列满返回 False、关停排空在途事件、
队列驱动真实入库管道(集成)。
"""
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.event_queue import EventQueue


def _wait(cond, timeout: float = 6.0) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.01)
    return False


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
    """集成:队列消费的事件走正常入库管道,落库成功。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

    from app.models import Base, Message
    from app.services.message_service import MessageService

    # 内存 SQLite 每个连接独立,StaticPool 共享同一连接(消费线程可见建表)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def fn(data):
        with Session() as s:
            MessageService(s, allowed_chat_ids=set()).handle_receive_event(data)

    q = EventQueue(process_fn=fn, retry_delays=(0.01,))
    q.start()
    event = P2ImMessageReceiveV1(
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
    q.submit(event)

    def _count() -> int:
        with Session() as s:
            return s.query(Message).count()

    assert _wait(lambda: _count() == 1)
    with Session() as s:
        row = s.query(Message).one()
        assert row.text == "hello queue"
    q.stop()
