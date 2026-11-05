"""H2-A 主题分类跨 worker 去重测试(DB 租约)。

验证:同 (chat, root_message_id) 的组只允许一个 worker 调度分类,
另一 worker 抢占失效(租约已建/在途/已 done)时不重复调 LLM;
分类成功标记 done、失败进入冷却;已带标签的历史组不重复调度。
使用共享内存 SQLite(StaticPool)模拟多 worker 同库。
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, Message, TopicClassifyLock
from app.services.topic_service import TopicService


class FakeLLM:
    """按队列返回标签;只剩一个时固定复用。"""

    def __init__(self, *labels: str):
        self.labels = list(labels)
        self.calls: list[str] = []

    def chat(self, system: str, user: str) -> str:
        self.calls.append(user)
        if len(self.labels) > 1:
            return self.labels.pop(0)
        return self.labels[0]


def _shared_session():
    """所有 session 共享同一内存库(StaticPool),模拟多 worker 同库访问。"""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _noop_submit(slot: dict):
    def _submit(*args, **kwargs):
        slot["n"] = slot.get("n", 0) + 1

    return _submit


def _seed_msg(Session, mid: str, root: str, chat: str = "oc") -> None:
    with Session() as s:
        s.add(
            Message(
                message_id=mid,
                chat_id=chat,
                root_message_id=root,
                sender_id="u1",
                sender_type="user",
                message_type="text",
                text=f"消息-{mid}",
                create_time="1700000000000",
                conversation_id=0,
            )
        )
        s.commit()


def test_cross_worker_schedule_dedup():
    """两 worker 同组各调度一次 → 只有先到者中标并提交执行。"""
    Session = _shared_session()
    svc_a = TopicService(session_factory=Session, llm=FakeLLM("音爆"))
    svc_b = TopicService(session_factory=Session, llm=FakeLLM("音爆"))
    sub_a, sub_b = {"n": 0}, {"n": 0}
    svc_a._executor.submit = _noop_submit(sub_a)
    svc_b._executor.submit = _noop_submit(sub_b)

    assert svc_a.schedule_group("oc", "r1") is True   # A 中标
    assert svc_b.schedule_group("oc", "r1") is False  # B 看到在途租约,跳过
    assert sub_a["n"] == 1
    assert sub_b["n"] == 0
    # 库里只有一条租约记录
    with Session() as s:
        rows = s.execute(
            select(TopicClassifyLock).where(TopicClassifyLock.root_message_id == "r1")
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "pending"


def test_done_lock_skips():
    """租约已 done(已分类完成)时,任何 worker 都不再调度。"""
    Session = _shared_session()
    with Session() as s:
        s.add(
            TopicClassifyLock(
                chat_id="oc",
                root_message_id="r2",
                status="done",
                claimed_at=datetime.now(timezone.utc),
            )
        )
        s.commit()
    svc = TopicService(session_factory=Session, llm=FakeLLM("音爆"))
    sub = {"n": 0}
    svc._executor.submit = _noop_submit(sub)
    assert svc.schedule_group("oc", "r2") is False
    assert sub["n"] == 0


def test_existing_topic_skips():
    """历史已带标签的组(无锁记录)→ 不重复分类,也不建锁。"""
    Session = _shared_session()
    _seed_msg(Session, "m1", "r3")
    with Session() as s:
        s.query(Message).filter(Message.message_id == "m1").update({"topic": "投屏"})
        s.commit()
    svc = TopicService(session_factory=Session, llm=FakeLLM("音爆"))
    sub = {"n": 0}
    svc._executor.submit = _noop_submit(sub)
    assert svc.schedule_group("oc", "r3") is False
    assert sub["n"] == 0
    with Session() as s:
        assert s.execute(select(TopicClassifyLock)).first() is None


def test_worker_marks_done_after_success():
    """分类成功 → 租约标记 done,消息写入标签。"""
    Session = _shared_session()
    _seed_msg(Session, "m1", "r4")
    with Session() as s:
        s.add(
            TopicClassifyLock(
                chat_id="oc",
                root_message_id="r4",
                status="pending",
                claimed_at=datetime.now(timezone.utc),
            )
        )
        s.commit()
    svc = TopicService(session_factory=Session, llm=FakeLLM("画面"))
    svc._classify_worker("oc", "r4")
    with Session() as s:
        row = s.execute(
            select(TopicClassifyLock).where(TopicClassifyLock.root_message_id == "r4")
        ).scalar_one()
        assert row.status == "done"
        assert row.retry_at is None
        msg = s.query(Message).filter(Message.message_id == "m1").one()
        assert msg.topic == "画面"


def test_worker_marks_retry_on_failure():
    """分类失败 → 租约保持 pending 并进入冷却(retry_at),不误标 done。"""
    Session = _shared_session()

    class BoomLLM:
        def chat(self, system: str, user: str) -> str:
            raise RuntimeError("llm down")

    _seed_msg(Session, "m1", "r5")
    with Session() as s:
        s.add(
            TopicClassifyLock(
                chat_id="oc",
                root_message_id="r5",
                status="pending",
                claimed_at=datetime.now(timezone.utc),
            )
        )
        s.commit()
    svc = TopicService(session_factory=Session, llm=BoomLLM())
    svc._classify_worker("oc", "r5")
    with Session() as s:
        row = s.execute(
            select(TopicClassifyLock).where(TopicClassifyLock.root_message_id == "r5")
        ).scalar_one()
        assert row.status == "pending"
        assert row.retry_at is not None
