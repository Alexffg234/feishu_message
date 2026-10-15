"""同主题归组集成测试(内存 SQLite + 假 LLM)。

验证:LLM 打完标签后,同群内相同 topic 的回复链组归并到最早组;
方向正确(原问题块保留为根)、异标签不并、开关可关、归并后回复仍归组、
幂等与 dry-run 规划只读、对话/回复边/统计完整迁移。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Conversation, Message, MessageLink
from app.schemas.message import MessageCreate
from app.services.message_service import MessageService
from app.services.topic_service import TopicService
from app.services.group_merge import merge_groups, merge_topic_group, plan_merges

T0 = 1700000000000
CHAT = "oc_topic"


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


def _setup():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    svc = MessageService(session, allowed_chat_ids=set())
    return svc, session, Session


def _msg(mid, sender, ts, parent="", root="", text=None):
    return MessageCreate(
        message_id=mid,
        chat_id=CHAT,
        sender_id=sender,
        sender_type="user",
        message_type="text",
        text=text if text is not None else f"msg-{mid}",
        create_time=str(ts),
        parent_message_id=parent,
        root_message_id=root,
    )


def _two_groups(svc, session):
    """两个独立组:m1(u1 问题块) / m2(u2 无回复反馈,间隔超轮次窗)。"""
    svc.save_message(_msg("m1", "u1", T0, text="电视看久了有音爆声"))
    svc.save_message(_msg("m2", "u2", T0 + 120_000, text="视频里面的声音都是爆音吗"))
    assert len(session.query(Conversation).all()) == 2


def _topic_svc(Session, *labels):
    return TopicService(session_factory=Session, llm=FakeLLM(*labels))


def _msgs(session):
    return {m.message_id: m for m in session.query(Message).all()}


def test_same_topic_merges_into_oldest():
    svc, session, Session = _setup()
    _two_groups(svc, session)

    ts = _topic_svc(Session, "音爆")
    ts.classify_group(CHAT, "m1")
    ts.classify_group(CHAT, "m2")  # 同标签 → 归并到最早组 m1

    convs = session.query(Conversation).all()
    assert len(convs) == 1, f"应归并为 1 个对话,实际 {len(convs)}"
    conv = convs[0]
    assert conv.root_message_id == "m1", "最早组(原问题块)应保留为根"
    assert conv.message_count == 2
    assert conv.participant_count == 2
    assert conv.topic == "音爆"

    msgs = _msgs(session)
    assert msgs["m1"].root_message_id == "m1"
    assert msgs["m2"].root_message_id == "m1"
    assert msgs["m2"].conversation_id == conv.id
    assert msgs["m2"].topic == "音爆"
    print("same-topic merge OK")


def test_newer_classified_first_still_merges_into_oldest():
    svc, session, Session = _setup()
    _two_groups(svc, session)

    ts = _topic_svc(Session, "音爆")
    ts.classify_group(CHAT, "m2")  # 新组先分类,此时无同标签组
    assert len(session.query(Conversation).all()) == 2
    ts.classify_group(CHAT, "m1")  # 老组后分类 → m2 并入 m1

    convs = session.query(Conversation).all()
    assert len(convs) == 1
    assert convs[0].root_message_id == "m1"
    print("reverse classify order OK")


def test_different_topics_no_merge():
    svc, session, Session = _setup()
    _two_groups(svc, session)

    ts = _topic_svc(Session, "音爆", "投屏")
    ts.classify_group(CHAT, "m1")
    ts.classify_group(CHAT, "m2")

    assert len(session.query(Conversation).all()) == 2
    print("different topics OK")


def test_merge_disabled_no_merge():
    from app.core.config import get_settings

    settings = get_settings()
    old = settings.topic_merge_enabled
    settings.topic_merge_enabled = False
    try:
        svc, session, Session = _setup()
        _two_groups(svc, session)
        ts = _topic_svc(Session, "音爆")
        ts.classify_group(CHAT, "m1")
        ts.classify_group(CHAT, "m2")
        assert len(session.query(Conversation).all()) == 2, "开关关闭不应归并"
        # 但标签照常打
        msgs = _msgs(session)
        assert msgs["m1"].topic == "音爆" and msgs["m2"].topic == "音爆"
    finally:
        settings.topic_merge_enabled = old
    print("disabled OK")


def test_reply_after_merge_routes_to_target():
    svc, session, Session = _setup()
    _two_groups(svc, session)
    ts = _topic_svc(Session, "音爆")
    ts.classify_group(CHAT, "m1")
    ts.classify_group(CHAT, "m2")

    # 归并后,quote 回复 m2(已并入 m1)仍归入 m1 组
    svc.save_message(_msg("r1", "u3", T0 + 300_000, parent="m2"))
    msgs = _msgs(session)
    assert msgs["r1"].root_message_id == "m1"
    convs = session.query(Conversation).all()
    assert len(convs) == 1
    assert convs[0].message_count == 3
    assert convs[0].participant_count == 3
    print("reply after merge OK")


def test_links_and_tree_preserved():
    svc, session, Session = _setup()
    svc.save_message(_msg("m1", "u1", T0, text="问题描述"))
    svc.save_message(_msg("m2", "u2", T0 + 120_000, text="反馈1"))
    svc.save_message(
        _msg("m2b", "u3", T0 + 130_000, parent="m2", text="反馈1 的回复")
    )

    ts = _topic_svc(Session, "音爆")
    ts.classify_group(CHAT, "m1")
    ts.classify_group(CHAT, "m2")

    convs = session.query(Conversation).all()
    assert len(convs) == 1
    conv = convs[0]
    assert conv.message_count == 3

    # 源组的回复边已移入目标对话
    links = session.query(MessageLink).all()
    assert len(links) == 1
    assert links[0].conversation_id == conv.id
    assert links[0].child_message_id == "m2b"

    # 树形结构:目标对话下两个根(m1 与 m2),m2 下挂 m2b
    from app.dao.conversation_repo import ConversationRepo

    tree = ConversationRepo(session).list_tree(conv.id)
    assert tree["message_count"] == 3
    roots = tree["roots"]
    assert len(roots) == 2, f"应有 2 个根节点,实际 {len(roots)}"
    root_ids = {r["message"]["message_id"] for r in roots}
    assert root_ids == {"m1", "m2"}
    m2_node = next(r for r in roots if r["message"]["message_id"] == "m2")
    assert [c["message"]["message_id"] for c in m2_node["children"]] == ["m2b"]
    print("links & tree OK")


def test_idempotent_and_plan_readonly():
    svc, session, Session = _setup()
    _two_groups(svc, session)
    # 只打标签不归并(回填 dry-run 前的路径)
    ts = _topic_svc(Session, "音爆")
    ts.classify_group(CHAT, "m1", merge=False)
    ts.classify_group(CHAT, "m2", merge=False)
    assert len(session.query(Conversation).all()) == 2

    # 规划只读:返回 (源,目标) 对,库不变
    pairs = plan_merges(session, CHAT, "音爆")
    assert pairs == [("m2", "m1")]
    assert len(session.query(Conversation).all()) == 2

    # 执行归并
    done = merge_topic_group(session, CHAT, "音爆")
    assert done == [("m2", "m1")]
    assert len(session.query(Conversation).all()) == 1

    # 幂等:再跑一次无操作
    assert merge_topic_group(session, CHAT, "音爆") == []
    assert merge_groups(session, CHAT, "m2", "m1", "音爆") is False
    assert merge_groups(session, CHAT, "m1", "m1", "音爆") is False
    assert len(session.query(Conversation).all()) == 1
    print("idempotent & plan readonly OK")


def test_merge_prompt_carries_topic_samples():
    svc, session, Session = _setup()
    _two_groups(svc, session)
    llm = FakeLLM("音爆")
    ts = TopicService(session_factory=Session, llm=llm)
    ts.classify_group(CHAT, "m1")
    ts.classify_group(CHAT, "m2")
    # 第二次分类的提示词应带已有主题摘要
    assert len(llm.calls) == 2
    assert "已有主题" in llm.calls[1]
    assert "音爆" in llm.calls[1]
    print("prompt samples OK")


def test_backfill_plan_pairs():
    from scripts.rethread_backfill import plan

    svc, session, Session = _setup()
    _two_groups(svc, session)
    ts = _topic_svc(Session, "音爆")
    ts.classify_group(CHAT, "m1", merge=False)
    ts.classify_group(CHAT, "m2", merge=False)

    pairs = plan(session, None)
    assert pairs == [(CHAT, "音爆", "m2", "m1")], pairs
    # 规划只读,库未变
    assert len(session.query(Conversation).all()) == 2
    print("backfill plan OK")


def test_attachment_only_group_skipped():
    """纯附件组(无文本)不分类,不产「图片」这类泛标签。"""
    from app.schemas.message import MessageCreate

    svc, session, Session = _setup()
    svc.save_message(
        MessageCreate(
            message_id="p1",
            chat_id=CHAT,
            sender_id="u1",
            sender_type="user",
            message_type="image",
            text="",
            create_time=str(T0),
        )
    )
    llm = FakeLLM("图片")
    ts = TopicService(session_factory=Session, llm=llm)
    assert ts.classify_group(CHAT, "p1") == ""
    assert llm.calls == [], "纯附件组不应调用 LLM"
    assert _msgs(session)["p1"].topic == ""
    print("attachment-only skip OK")


def test_reclassify_backfills_unlabeled_replies():
    """早退路径:组内分类后新到达的未标记消息应被回填 topic。"""
    from app.schemas.message import MessageCreate

    svc, session, Session = _setup()
    svc.save_message(_msg("m1", "u1", T0, text="电视看久了有音爆声"))
    svc.save_message(_msg("m2", "u2", T0 + 5_000, parent="m1", text="收到，我查一下"))
    ts = _topic_svc(Session, "音爆")
    ts.classify_group(CHAT, "m1")

    svc.save_message(
        MessageCreate(
            message_id="m3",
            chat_id=CHAT,
            sender_id="u1",
            sender_type="user",
            message_type="image",
            text="",
            create_time=str(T0 + 60_000),
            parent_message_id="m1",
        )
    )
    assert _msgs(session)["m3"].topic == "", "新回复入库时尚无标签"
    ts.classify_group(CHAT, "m1")  # 走早退分支,应回填组内消息
    assert _msgs(session)["m3"].topic == "音爆"
    assert len(session.query(Conversation).all()) == 1
    print("backfill unlabeled replies OK")


def test_topic_openings_uses_earliest_meaningful():
    from app.dao.message_repo import MessageRepo

    svc, session, Session = _setup()
    svc.save_message(_msg("o1", "u1", T0, text="收到"))
    svc.save_message(_msg("o2", "u1", T0 + 60_000, text="这个电视亮度无法调节"))
    repo = MessageRepo(session)
    repo.set_topic_for_group(CHAT, "o1", "亮度")
    repo.set_topic_for_group(CHAT, "o2", "亮度")
    session.commit()

    samples = dict(repo.list_topic_openings(chat_id=CHAT))
    assert samples == {"亮度": "这个电视亮度无法调节"}, samples
    print("topic openings OK")


def test_classify_prompt_carries_preceding_context():
    """组前最近消息(含其话题标签)应进入分类提示词。"""
    svc, session, Session = _setup()
    svc.save_message(_msg("k1", "u1", T0, text="帮看下这个电视，关机之后会自动开机"))
    _topic_svc(Session, "自动开机").classify_group(CHAT, "k1")

    llm = FakeLLM("自动开机")
    ts2 = TopicService(session_factory=Session, llm=llm)
    svc.save_message(_msg("k2", "u2", T0 + 120_000, text="是不是本控按键卡主了"))
    ts2.classify_group(CHAT, "k2")

    assert len(llm.calls) == 1
    assert "本组之前的最近消息" in llm.calls[0]
    assert "自动开机" in llm.calls[0], "前文消息应带其话题标注"
    # 沿用前文标签 → 并入最早组
    assert len(session.query(Conversation).all()) == 1
    print("preceding context OK")


if __name__ == "__main__":
    test_same_topic_merges_into_oldest()
    test_newer_classified_first_still_merges_into_oldest()
    test_different_topics_no_merge()
    test_merge_disabled_no_merge()
    test_reply_after_merge_routes_to_target()
    test_links_and_tree_preserved()
    test_idempotent_and_plan_readonly()
    test_merge_prompt_carries_topic_samples()
    test_backfill_plan_pairs()
    test_attachment_only_group_skipped()
    test_reclassify_backfills_unlabeled_replies()
    test_topic_openings_uses_earliest_meaningful()
    test_classify_prompt_carries_preceding_context()
    print("ALL TOPIC MERGE TESTS PASSED")
