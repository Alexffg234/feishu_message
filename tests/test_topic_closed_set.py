"""闭集判别 + 处理阶段分维度 的测试。

覆盖两件事:
1. stage(处理阶段)是纯规则、与 topic 正交:疑问句里的「推送」不算已推送、
   「恢复出厂」不算已解决、纯客套句不产生阶段;
2. topic 走闭集判别:配置了标准标签后,闭集外的标签(含动作词、UNKNOWN)
   一律不写库,保留未分类;JSON 输出解析置信度,旧纯文本格式仍兼容。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Conversation, Message, TopicGroup
from app.schemas.message import MessageCreate
from app.services.message_service import MessageService
from app.services.stage import (
    STAGE_DOING,
    STAGE_ONSITE,
    STAGE_PENDING,
    STAGE_PUSHED,
    STAGE_RESOLVED,
    detect_stage,
)
from app.services.topic_service import TopicService, get_standard_tree

T0 = 1700000000000
CHAT = "oc_closed"


class FakeLLM:
    """按队列返回预设输出;只剩一个时固定复用。"""

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.calls: list[str] = []

    def chat(self, system: str, user: str) -> str:
        self.calls.append(user)
        if len(self.replies) > 1:
            return self.replies.pop(0)
        return self.replies[0]


def _setup(standards: dict[str, list[str]] | None = None):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    if standards:
        for i, (name, topics) in enumerate(standards.items()):
            session.add(TopicGroup(name=name, topics=topics, sort_order=i))
        session.commit()
    svc = MessageService(session, allowed_chat_ids=set())
    return svc, session, Session


def _msg(mid, ts, text, sender="u1", parent=""):
    return MessageCreate(
        message_id=mid,
        chat_id=CHAT,
        sender_id=sender,
        sender_type="user",
        message_type="text",
        text=text,
        create_time=str(ts),
        parent_message_id=parent,
    )


def _rows(session):
    return {m.message_id: m for m in session.query(Message).all()}


# ---------------------------------------------------------------- stage 规则
def test_stage_question_is_not_pushed():
    """「能推送解决吗」是提问,不能算已完成推送。"""
    assert detect_stage(["能后台推送解决吗"]) == ""
    assert detect_stage(["这个能推送一下吗，谢谢"]) == ""


def test_stage_resolved_beats_others():
    assert detect_stage(["已推了一版", "升级后正常了"]) == STAGE_RESOLVED
    # 恢复出厂是重置动作,不是问题解决
    assert detect_stage(["恢复出厂设置后还是一样"]) != STAGE_RESOLVED


def test_stage_pushed_and_onsite():
    assert detect_stage(["推了个新版本，升级后观察"]) == STAGE_PUSHED
    assert detect_stage(["师傅上门了，已换板卡"]) == STAGE_ONSITE


def test_stage_doing_and_pending():
    assert detect_stage(["已开远程，稍后回复"]) == STAGE_DOING
    assert detect_stage(["电视观看时黑屏"]) == STAGE_PENDING
    # 纯客套句不构成阶段
    assert detect_stage(["好的", "收到谢谢"]) == ""


def test_stage_written_on_save_and_refreshed():
    """入库即写 stage,后续消息到达会刷新(不依赖是否已打标签)。"""
    svc, session, _Session = _setup()
    svc.save_message(_msg("m1", T0, "这个电视投屏搜不到设备，麻烦看下"))
    rows = _rows(session)
    assert rows["m1"].stage == STAGE_PENDING
    conv = session.query(Conversation).one()
    assert conv.stage == STAGE_PENDING

    svc.save_message(_msg("m2", T0 + 60_000, "已推了一版，升级后再观察", parent="m1"))
    rows = _rows(session)
    assert rows["m2"].stage == STAGE_PUSHED
    assert rows["m1"].stage == STAGE_PUSHED, "同组消息应共享同一阶段"
    session.expire_all()
    assert session.query(Conversation).one().stage == STAGE_PUSHED


# ---------------------------------------------------------------- 闭集判别
def test_standard_tree_shape():
    """大类 > 子类 的层级被保留,叶子标签才是可选集合。"""
    _svc, session, _Session = _setup(
        {"画质类问题": [], "画面模糊类问题": ["画面模糊"], "投屏问题": ["投屏卡顿"]}
    )
    session.query(TopicGroup).filter_by(name="画面模糊类问题").update(
        {"parent": "画质类问题"}
    )
    session.commit()
    tree = get_standard_tree(session)
    names = [n["name"] for n in tree]
    assert "画质类问题" in names and "画面模糊类问题" not in names
    top = next(n for n in tree if n["name"] == "画质类问题")
    assert top["sub"][0]["topics"] == ["画面模糊"]


def test_in_set_label_written_with_confidence():
    svc, session, Session = _setup({"投屏问题": ["投屏找不到设备", "投屏卡顿"]})
    svc.save_message(_msg("m1", T0, "投屏搜不到电视，同一个热点也搜不到"))
    ts = TopicService(
        session_factory=Session,
        llm=FakeLLM('{"topic": "投屏找不到设备", "confidence": 0.93}'),
    )
    assert ts.classify_group(CHAT, "m1") == "投屏找不到设备"
    session.expire_all()
    row = _rows(session)["m1"]
    assert row.topic == "投屏找不到设备"
    assert abs(row.topic_conf - 0.93) < 1e-6


def test_out_of_set_label_kept_unclassified():
    """闭集外的动作词(历史遗留的「已推」)不再写进 topic。"""
    svc, session, Session = _setup({"投屏问题": ["投屏找不到设备", "投屏卡顿"]})
    svc.save_message(_msg("m1", T0, "已推了一版，升级后观察"))
    ts = TopicService(
        session_factory=Session,
        llm=FakeLLM('{"topic": "已推", "confidence": 0.8}'),
    )
    assert ts.classify_group(CHAT, "m1") == ""
    session.expire_all()
    row = _rows(session)["m1"]
    assert row.topic == "", "闭集外标签不得入库"
    assert row.stage == STAGE_PUSHED, "动作词应落到 stage 维度"
    assert session.query(Conversation).one().topic == ""


def test_unknown_kept_unclassified():
    svc, session, Session = _setup({"投屏问题": ["投屏找不到设备"]})
    svc.save_message(_msg("m1", T0, "商量一下下季度的排产计划"))
    ts = TopicService(session_factory=Session, llm=FakeLLM('{"topic": "UNKNOWN"}'))
    assert ts.classify_group(CHAT, "m1") == ""
    session.expire_all()
    assert _rows(session)["m1"].topic == ""


def test_no_standards_keeps_legacy_freeform():
    """未配置标准标签时维持自由标签(兼容存量部署与历史测试)。"""
    svc, session, Session = _setup()
    svc.save_message(_msg("m1", T0, "电视看久了有音爆声"))
    ts = TopicService(session_factory=Session, llm=FakeLLM("音爆"))
    assert ts.classify_group(CHAT, "m1") == "音爆"


def test_prompt_carries_closed_set():
    svc, session, Session = _setup({"投屏问题": ["投屏找不到设备"]})
    svc.save_message(_msg("m1", T0, "投屏搜不到电视"))
    llm = FakeLLM('{"topic": "投屏找不到设备", "confidence": 0.9}')
    TopicService(session_factory=Session, llm=llm).classify_group(CHAT, "m1")
    prompt = llm.calls[0]
    assert "可选标签" in prompt and "投屏找不到设备" in prompt
    assert "已有主题" in prompt and "本组之前的最近消息" in prompt


# ---------------------------------------------------------------- JSON 解析
def test_parse_decision_variants():
    parse = TopicService._parse_decision
    assert parse('{"topic": "投屏卡顿", "confidence": 0.7}') == ("投屏卡顿", 0.7)
    assert parse('```json\n{"topic": "投屏卡顿", "confidence": 0.4}\n```') == (
        "投屏卡顿",
        0.4,
    )
    # 混在解释文字里的 JSON 也能抓出来
    assert parse('判定如下 {"topic": "投屏卡顿", "confidence": 0.5} 完毕')[0] == "投屏卡顿"
    # 旧格式纯文本:整段当标签,置信度未知
    assert parse("投屏卡顿") == ("投屏卡顿", 0.0)
    # 越界置信度被夹到 0~1
    assert parse('{"topic": "x", "confidence": 3}')[1] == 1.0
    assert parse("") == ("", 0.0)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name} OK")
