"""会话缝合测试(内存 SQLite + 假 LLM)。

验证:按问题处理生命周期(提问→排查→对话→解决)把无回复链、标签缺失/
垃圾标签的相邻会话合并;方向正确(最早会话为根)、异问题不并、
跨窗口链式关联可传递、无 LLM 降级、规划只读、幂等、语气词标签守卫。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Conversation, Message
from app.schemas.message import MessageCreate
from app.services.conv_stitch import _parse_stitch_result, plan_stitches, stitch_chat
from app.services.message_service import MessageService

T0 = 1700000000000
CHAT = "oc_stitch"


class FakeLLM:
    """按队列返回输出;只剩一个时固定复用。"""

    def __init__(self, *outputs: str):
        self.outputs = list(outputs)
        self.calls: list[str] = []

    def chat(self, system: str, user: str) -> str:
        self.calls.append(user)
        if len(self.outputs) > 1:
            return self.outputs.pop(0)
        return self.outputs[0]


def _setup():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    svc = MessageService(session, allowed_chat_ids=set())
    return svc, session, Session


def _msg(mid, sender, ts, text):
    return MessageCreate(
        message_id=mid,
        chat_id=CHAT,
        sender_id=sender,
        sender_type="user",
        message_type="text",
        text=text,
        create_time=str(ts),
    )


def _qr_case(svc, session):
    """复刻真实拆散案例:3 个独立会话(无回复链),同一 QR 充值问题。"""
    svc.save_message(_msg("m1", "u1", T0, text="住居美学馆充值二维码扫描不了"))
    svc.save_message(_msg("m2", "u2", T0 + 120_000, text="帮看看"))
    svc.save_message(
        _msg(
            "m3",
            "u3",
            T0 + 300_000,
            text="后台维护暂时无法充值,先发下用户的海尔账号,先送用户一个月会员",
        )
    )
    assert len(session.query(Conversation).all()) == 3


def test_parse_stitch_result():
    assert _parse_stitch_result("1,2,3", 5) == {1, 2, 3}
    assert _parse_stitch_result("无", 5) == set()
    assert _parse_stitch_result(" 1 ， 2 ", 5) == {1, 2}
    assert _parse_stitch_result("1,9", 5) == {1}, "越界序号忽略"
    assert _parse_stitch_result("无,2", 5) == {2}
    assert _parse_stitch_result("", 5) == set()
    print("parse OK")


def test_lifecycle_stitch_merges_three_conversations():
    svc, session, Session = _setup()
    _qr_case(svc, session)
    llm = FakeLLM("1,2,3")

    pairs = plan_stitches(session, CHAT, llm)
    convs = {c.root_message_id: c.id for c in session.query(Conversation).all()}
    assert pairs == [
        (convs["m2"], convs["m1"]),
        (convs["m3"], convs["m1"]),
    ], pairs
    # 规划只读
    assert len(session.query(Conversation).all()) == 3

    done = stitch_chat(session, CHAT, llm)
    assert len(done) == 2

    convs = session.query(Conversation).all()
    assert len(convs) == 1, f"应合并为 1 个会话,实际 {len(convs)}"
    conv = convs[0]
    assert conv.root_message_id == "m1", "最早会话应保留为根"
    assert conv.message_count == 3
    assert conv.participant_count == 3

    msgs = {m.message_id: m for m in session.query(Message).all()}
    assert msgs["m2"].root_message_id == "m1"
    assert msgs["m3"].conversation_id == conv.id
    print("lifecycle stitch OK")


def test_stitch_idempotent():
    svc, session, Session = _setup()
    _qr_case(svc, session)
    stitch_chat(session, CHAT, FakeLLM("1,2,3"))
    assert len(session.query(Conversation).all()) == 1
    # 只剩 1 个会话,再跑无操作
    assert stitch_chat(session, CHAT, FakeLLM("无")) == []
    assert len(session.query(Conversation).all()) == 1
    print("idempotent OK")


def test_different_problems_no_stitch():
    svc, session, Session = _setup()
    svc.save_message(_msg("m1", "u1", T0, text="电视看久了有爆音"))
    svc.save_message(_msg("m2", "u2", T0 + 120_000, text="电视画质模糊"))
    llm = FakeLLM("无")

    assert plan_stitches(session, CHAT, llm) == []
    assert len(session.query(Conversation).all()) == 2
    print("different problems OK")


def test_no_llm_returns_empty():
    svc, session, Session = _setup()
    _qr_case(svc, session)
    assert plan_stitches(session, CHAT, None) == []
    assert stitch_chat(session, CHAT, None) == []
    assert len(session.query(Conversation).all()) == 3
    print("no llm OK")


def test_cross_window_chain_merges():
    """窗口重叠+并查集:c4,c5 与 c5,c6 分别被判合并 → c4/c5/c6 传递合并。"""
    svc, session, Session = _setup()
    for i in range(1, 8):
        svc.save_message(_msg(f"m{i}", f"u{i}", T0 + i * 120_000, text=f"消息{i}"))
    assert len(session.query(Conversation).all()) == 7

    # plan 与 stitch_chat 内部 plan 各消费两次窗口输出,队列给足
    llm = FakeLLM("4,5", "1,2", "4,5", "1,2")  # 窗口1: 全局 m4,m5;窗口2: 全局 m5,m6
    pairs = plan_stitches(session, CHAT, llm)
    convs = {c.root_message_id: c.id for c in session.query(Conversation).all()}
    assert len(llm.calls) == 2
    assert pairs == [
        (convs["m5"], convs["m4"]),
        (convs["m6"], convs["m4"]),
    ], pairs

    stitch_chat(session, CHAT, llm)
    assert len(session.query(Conversation).all()) == 5, "m4/m5/m6 应合并为 1"
    conv = next(
        c for c in session.query(Conversation).all() if c.root_message_id == "m4"
    )
    assert conv.message_count == 3
    print("cross-window chain OK")


def test_single_conversation_no_stitch():
    svc, session, Session = _setup()
    svc.save_message(_msg("m1", "u1", T0, text="只有一个会话"))
    assert plan_stitches(session, CHAT, FakeLLM("无")) == []
    print("single conv OK")


def test_stop_label_guard():
    from app.services.topic_service import _clean_label

    assert _clean_label("好的") == ""
    assert _clean_label("收到") == ""
    assert _clean_label("OK") == ""
    assert _clean_label("嗯") == ""
    assert _clean_label("投屏") == "投屏"
    assert _clean_label("远场语音唤不醒") == "远场语音唤不醒"
    print("stop label guard OK")


if __name__ == "__main__":
    test_parse_stitch_result()
    test_lifecycle_stitch_merges_three_conversations()
    test_stitch_idempotent()
    test_different_problems_no_stitch()
    test_no_llm_returns_empty()
    test_cross_window_chain_merges()
    test_single_conversation_no_stitch()
    test_stop_label_guard()
    print("ALL CONV STITCH TESTS PASSED")
