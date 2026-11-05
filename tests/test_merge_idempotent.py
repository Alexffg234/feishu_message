"""H2-B 同主题归并并发幂等测试。

验证:多 worker/线程并发对同一 (source→target) 调用 merge_groups 时,
仅一次真归并,重复调用幂等安全(不重复删源对话、不重复搬消息),
最终状态一致无异常。

说明:SQLite 下 SELECT ... FOR UPDATE 为 no-op,真正跨 worker 的行锁语义
由 MySQL/PostgreSQL 提供;本测试按约定用「并发线程 + 串行化」验证
幂等重入仍安全(进程内 _merge_lock + 持锁后重读源组双保险)。
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Conversation, Message
from app.services.group_merge import merge_groups

CHAT = "oc_merge"


def _seed(Session) -> None:
    """两个同 tag 组:m1(目标/最早) 与 m2(源)。"""
    with Session() as s:
        target = Conversation(
            chat_id=CHAT,
            root_message_id="m1",
            root_sender_id="u1",
            status="open",
            topic="音爆",
            started_at_ms=0,
            last_message_at_ms=0,
        )
        s.add(target)
        s.flush()
        source = Conversation(
            chat_id=CHAT,
            root_message_id="m2",
            root_sender_id="u2",
            status="open",
            topic="音爆",
            started_at_ms=0,
            last_message_at_ms=0,
        )
        s.add(source)
        s.flush()
        s.add(
            Message(
                message_id="m1", chat_id=CHAT, root_message_id="m1", sender_id="u1",
                sender_type="user", message_type="text", text="电视爆音",
                create_time="1700000000000", topic="音爆", conversation_id=target.id,
            )
        )
        s.add(
            Message(
                message_id="m2", chat_id=CHAT, root_message_id="m2", sender_id="u2",
                sender_type="user", message_type="text", text="我的也爆",
                create_time="1700000001000", topic="音爆", conversation_id=source.id,
            )
        )
        s.commit()


def test_concurrent_merge_idempotent(tmp_path) -> None:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'merge.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    _seed(Session)

    results: list[bool] = []
    results_lock = threading.Lock()
    errors: list[str] = []

    def worker() -> None:
        with Session() as s:
            try:
                ok = merge_groups(s, CHAT, "m2", "m1", topic="音爆")
                with results_lock:
                    results.append(ok)
            except Exception as e:  # noqa: BLE001
                with results_lock:
                    errors.append(repr(e))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    # 无异常
    assert not errors, errors
    # 恰好一次真归并,另一次幂等返回 False
    assert results.count(True) == 1, results
    assert results.count(False) == 1, results

    # 最终状态:m1 组两条消息,m2 组为空;仅剩一个对话(源对话已删)
    with Session() as s:
        assert s.query(Message).count() == 2
        msgs = s.query(Message).all()
        assert all(m.root_message_id == "m1" for m in msgs)
        convs = s.query(Conversation).all()
        assert len(convs) == 1
        assert convs[0].root_message_id == "m1"
        # 归并后统计正确
        assert convs[0].message_count == 2
        assert convs[0].participant_count == 2
