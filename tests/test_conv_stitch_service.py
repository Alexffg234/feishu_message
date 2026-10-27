"""会话缝合后台调度测试(内存 SQLite + 假 LLM)。

验证:run_round 合并水位线变化的群、水位线不变跳过省 LLM、
新消息到达触发再判定、无 LLM 降级、缝合的续聊不画横杠(同一案例不断句)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Conversation, Message
from app.schemas.message import MessageCreate
from app.services.conv_stitch_service import ConvStitchService
from app.services.message_service import MessageService

T0 = 1700000000000
CHAT = "oc_stitch_svc"


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


def _three_case(svc, session):
    """3 个独立会话(无回复链),同一问题。"""
    svc.save_message(_msg("m1", "u1", T0, text="住居美学馆充值二维码扫描不了"))
    svc.save_message(_msg("m2", "u2", T0 + 120_000, text="帮看看"))
    svc.save_message(
        _msg("m3", "u3", T0 + 300_000, text="后台维护暂时无法充值,先送用户一个月会员")
    )
    assert len(session.query(Conversation).all()) == 3


def test_run_round_merges_and_watermark_skips():
    svc, session, Session = _setup()
    _three_case(svc, session)
    llm = FakeLLM("1,2,3", "无")
    service = ConvStitchService(session_factory=Session, llm=llm, interval_seconds=999)

    n = service.run_round()
    assert n == 2, f"应合并 2 对,实际 {n}"
    assert len(session.query(Conversation).all()) == 1
    assert len(llm.calls) == 1

    # 无新消息:水位线不变,跳过,不再调 LLM
    assert service.run_round() == 0
    assert len(llm.calls) == 1, "水位线未变不应再调 LLM"
    print("round + watermark OK")


def test_new_message_triggers_replan():
    svc, session, Session = _setup()
    _three_case(svc, session)
    llm = FakeLLM("1,2,3", "1,2")
    service = ConvStitchService(session_factory=Session, llm=llm, interval_seconds=999)
    assert service.run_round() == 2
    assert len(llm.calls) == 1

    # 新消息(同一案例的后续碎片)→ 水位线变化,重新判定并合并
    svc.save_message(_msg("m4", "u1", T0 + 600_000, text="还是不行,麻烦再看看"))
    session.commit()
    assert service.run_round() == 1
    assert len(llm.calls) == 2
    assert len(session.query(Conversation).all()) == 1
    print("replan on new message OK")


def test_no_llm_noop():
    svc, session, Session = _setup()
    _three_case(svc, session)
    service = ConvStitchService(session_factory=Session, llm=None, interval_seconds=999)
    assert service.enabled is False
    assert service.run_round() == 0
    assert len(session.query(Conversation).all()) == 3
    print("no llm noop OK")


def test_stitch_no_block_start_for_continuation():
    """同标签续聊被缝合 → 不打横杠标记(同一问题不断句)。"""
    svc, session, Session = _setup()
    svc.save_message(_msg("m1", "u1", T0, text="电视爆音,怎么处理"))
    svc.save_message(_msg("m2", "u2", T0 + 120_000, text="爆音还在,换了HDMI线"))
    # 两个会话都有相同标签(模拟已分类的续聊)
    for root in ("m1", "m2"):
        conv = session.query(Conversation).filter_by(root_message_id=root).one()
        conv.topic = "爆音"
        for m in session.query(Message).filter(Message.root_message_id == root):
            m.topic = "爆音"
    session.commit()

    llm = FakeLLM("1,2")
    service = ConvStitchService(session_factory=Session, llm=llm, interval_seconds=999)
    assert service.run_round() == 1
    assert len(session.query(Conversation).all()) == 1

    flags = [m.block_start for m in session.query(Message).all()]
    assert all(f == 0 for f in flags), f"续聊缝合不应打横杠标记,实际 {flags}"
    print("no block start OK")


if __name__ == "__main__":
    test_run_round_merges_and_watermark_skips()
    test_new_message_triggers_replan()
    test_no_llm_noop()
    test_stitch_no_block_start_for_continuation()
    print("ALL CONV STITCH SERVICE TESTS PASSED")
