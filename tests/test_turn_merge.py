"""轮次合并(同一人连发多条)集成测试(内存 SQLite)。

验证:同一发送者短间隔连发的独立消息并入同一消息块/对话;
间隔超窗、无关者插话时不合并;关闭合并时各自成组。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Conversation, Message
from app.services.message_service import MessageService
from app.schemas.message import MessageCreate

T0 = 1700000000000


def _make_service():
    from app.core.config import get_settings

    # 固定窗口 30s,不受本地/服务器 .env 配置影响
    get_settings().turn_merge_seconds = 30
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    svc = MessageService(session, allowed_chat_ids=set())
    return svc, session


def _msg(mid, sender, ts, parent="", root=""):
    return MessageCreate(
        message_id=mid,
        chat_id="oc_turn",
        sender_id=sender,
        sender_type="user",
        message_type="text",
        text=f"msg-{mid}",
        create_time=str(ts),
        parent_message_id=parent,
        root_message_id=root,
    )


def _convs(session):
    return session.query(Conversation).all()


def test_consecutive_same_sender_merge():
    svc, session = _make_service()
    svc.save_message(_msg("a1", "u1", T0))
    svc.save_message(_msg("a2", "u1", T0 + 10_000))   # +10s,同发送者 → 并入 a1 块
    convs = _convs(session)
    assert len(convs) == 1, f"应为 1 个对话,实际 {len(convs)}"
    assert convs[0].root_message_id == "a1"
    assert convs[0].message_count == 2
    m2 = session.query(Message).filter(Message.message_id == "a2").one()
    assert m2.root_message_id == "a1"
    assert m2.conversation_id == convs[0].id
    print("consecutive merge OK")


def test_gap_beyond_window_new_block():
    svc, session = _make_service()
    svc.save_message(_msg("a1", "u1", T0))
    svc.save_message(_msg("a2", "u1", T0 + 40_000))   # +40s > 30s 窗口 → 新块
    assert len(_convs(session)) == 2
    print("gap beyond window OK")


def test_reply_in_between_still_merge():
    svc, session = _make_service()
    svc.save_message(_msg("m1", "u1", T0))
    svc.save_message(_msg("m2", "u1", T0 + 5_000))             # u1 连发 → 并入 m1
    svc.save_message(_msg("m3", "u2", T0 + 15_000, parent="m2"))  # u2 回复 m2
    svc.save_message(_msg("m4", "u1", T0 + 25_000))             # u1 继续,中间是 u2 的同组回复 → 并入
    convs = _convs(session)
    assert len(convs) == 1, f"应为 1 个对话,实际 {len(convs)}"
    assert convs[0].message_count == 4
    assert convs[0].participant_count == 2
    m4 = session.query(Message).filter(Message.message_id == "m4").one()
    assert m4.root_message_id == "m1"
    print("reply-in-between merge OK")


def test_unrelated_interrupt_no_merge():
    svc, session = _make_service()
    svc.save_message(_msg("n1", "u1", T0))
    svc.save_message(_msg("n2", "u2", T0 + 10_000))  # u2 无关独立消息(打断)
    svc.save_message(_msg("n3", "u1", T0 + 20_000))  # u1 继续,但中间有无关者 → 不合并
    assert len(_convs(session)) == 3
    n3 = session.query(Message).filter(Message.message_id == "n3").one()
    assert n3.root_message_id == "n3"
    print("unrelated interrupt OK")


def test_different_sender_no_merge():
    svc, session = _make_service()
    svc.save_message(_msg("p1", "u1", T0))
    svc.save_message(_msg("p2", "u2", T0 + 5_000))   # 不同发送者 → 不合并
    assert len(_convs(session)) == 2
    print("different sender OK")


def test_disabled_no_merge():
    from app.core.config import get_settings

    svc, session = _make_service()
    settings = get_settings()
    old = settings.turn_merge_seconds
    settings.turn_merge_seconds = 0
    try:
        svc.save_message(_msg("d1", "u1", T0))
        svc.save_message(_msg("d2", "u1", T0 + 5_000))  # 合并禁用 → 各自成组
        assert len(_convs(session)) == 2
    finally:
        settings.turn_merge_seconds = old
    print("disabled OK")


if __name__ == "__main__":
    test_consecutive_same_sender_merge()
    test_gap_beyond_window_new_block()
    test_reply_in_between_still_merge()
    test_unrelated_interrupt_no_merge()
    test_different_sender_no_merge()
    test_disabled_no_merge()
    print("ALL TURN MERGE TESTS PASSED")
