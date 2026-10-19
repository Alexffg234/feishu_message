"""会话拆分测试(内存 SQLite)。

验证:同主题归并混成的巨型对话能按计划拆回多组——消息重挂、回复边
跟随子消息、跨组边在目标对话树里呈现为根、统计重算、计划必须精确
覆盖(不重/不漏/不含外部消息)、keep 组根校验、dry-run 不改库。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Conversation, Message, MessageLink
from app.schemas.message import MessageCreate
from app.services.conversation_split import split_conversation
from app.services.group_merge import merge_groups
from app.services.message_service import MessageService

T0 = 1700000000000
CHAT = "oc_split"


def _setup():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    svc = MessageService(session, allowed_chat_ids=set())
    return svc, session, Session


def _msg(mid, sender, ts, text, parent=""):
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


def _merged_mega(session):
    """三个不同问题各成一个会话,再全部归并进 B(模拟同标签误并)。

    C 组仅 1 条独立消息;另手工补一条跨组回复边(c1 -> b2,父在 B),
    模拟真实数据中跨案例的回复边。
    """
    from app.models.conversation import MessageLink

    svc = MessageService(session, allowed_chat_ids=set())
    svc.save_message(_msg("a1", "u1", T0, text="U盘播放闪屏怎么办"))
    svc.save_message(_msg("a2", "u1", T0 + 1000, text="换了U盘也不行", parent="a1"))
    svc.save_message(_msg("b1", "u2", T0 + 2000, text="这台电视画面模糊"))
    svc.save_message(_msg("b2", "u2", T0 + 3000, text="能后台调参数吗", parent="b1"))
    svc.save_message(_msg("b3", "u3", T0 + 4000, text="我明天推送", parent="b2"))
    svc.save_message(_msg("c1", "u4", T0 + 5000, text="开机黑屏但是有声音"))
    assert len(session.query(Conversation).all()) == 3
    conv_b = session.query(Conversation).filter_by(root_message_id="b1").one()
    assert merge_groups(session, CHAT, "a1", "b1", "闪屏")
    assert merge_groups(session, CHAT, "c1", "b1", "闪屏")
    assert len(session.query(Conversation).all()) == 1
    session.add(
        MessageLink(
            conversation_id=conv_b.id,
            parent_message_id="b2",
            child_message_id="c1",
            parent_sender_id="u2",
            child_sender_id="u4",
            depth=1,
        )
    )
    session.commit()
    return conv_b.id


def _plan():
    return [
        {"root_message_id": "a1", "topic": "U盘播放闪屏", "message_ids": ["a1", "a2"]},
        {"root_message_id": "b1", "topic": "画面模糊", "keep": True,
         "message_ids": ["b1", "b2", "b3"]},
        {"root_message_id": "c1", "topic": "开机黑屏有声音", "message_ids": ["c1"]},
    ]


def test_split_dry_run_no_change():
    svc, session, _ = _setup()
    conv_id = _merged_mega(session)
    report = split_conversation(session, CHAT, conv_id, _plan(), apply=False)
    assert not report["applied"]
    assert len(report["groups"]) == 3
    assert [g["count"] for g in report["groups"]] == [2, 3, 1]
    # 未落库:仍是一个对话
    assert len(session.query(Conversation).all()) == 1
    m = session.query(Message).filter_by(message_id="a1").one()
    assert m.root_message_id == "b1"
    session.close()


def test_split_apply():
    svc, session, _ = _setup()
    conv_id = _merged_mega(session)
    report = split_conversation(session, CHAT, conv_id, _plan(), apply=True)
    assert report["applied"]
    groups = {g["root"]: g for g in report["groups"]}
    assert len(session.query(Conversation).all()) == 3

    # 消息重挂
    for mid, root in [("a1", "a1"), ("a2", "a1"), ("b1", "b1"),
                      ("b2", "b1"), ("b3", "b1"), ("c1", "c1")]:
        m = session.query(Message).filter_by(message_id=mid).one()
        assert m.root_message_id == root, mid
    conv_a = session.query(Conversation).filter_by(root_message_id="a1").one()
    conv_b = session.query(Conversation).filter_by(root_message_id="b1").one()
    conv_c = session.query(Conversation).filter_by(root_message_id="c1").one()
    assert conv_a.id == groups["a1"]["conversation_id"]
    assert conv_b.id == conv_id, "keep 组保留原对话行"
    assert conv_c.id == groups["c1"]["conversation_id"]
    # topic
    assert conv_a.topic == "U盘播放闪屏"
    assert conv_b.topic == "画面模糊"
    assert conv_c.topic == "开机黑屏有声音"
    # 组内消息标签同步(防旧标签让新消息分类时误并回旧组)
    for mid, topic in [("a1", "U盘播放闪屏"), ("b3", "画面模糊"),
                       ("c1", "开机黑屏有声音")]:
        m = session.query(Message).filter_by(message_id=mid).one()
        assert m.topic == topic, mid

    # 回复边跟随父消息;跨组边(c1->b2)落在父所在 b 组(渲染时被过滤)
    links = session.query(MessageLink).all()
    by_child = {l.child_message_id: l for l in links}
    assert by_child["a2"].conversation_id == conv_a.id
    assert by_child["b2"].conversation_id == conv_b.id
    assert by_child["b3"].conversation_id == conv_b.id
    assert by_child["c1"].conversation_id == conv_b.id

    # 统计
    assert conv_a.message_count == 2 and conv_a.participant_count == 1
    assert conv_b.message_count == 3 and conv_b.participant_count == 2
    assert conv_c.message_count == 1

    # C 的树:c1 为根(其回复父 b2 不在 C 内,不出现悬空节点)
    from app.dao.conversation_repo import ConversationRepo

    tree = ConversationRepo(session).list_tree(conv_c.id)
    assert tree["message_count"] == 1
    root_mids = {r["message"]["message_id"] for r in tree["roots"]}
    assert root_mids == {"c1"}
    session.close()


def test_split_plan_missing_message():
    _, session, _ = _setup()
    conv_id = _merged_mega(session)
    plan = _plan()
    plan[1]["message_ids"].remove("b3")
    try:
        split_conversation(session, CHAT, conv_id, plan)
        assert False, "应抛 ValueError"
    except ValueError as e:
        assert "漏了" in str(e)
    session.close()


def test_split_plan_duplicate_message():
    _, session, _ = _setup()
    conv_id = _merged_mega(session)
    plan = _plan()
    plan[0]["message_ids"].append("b1")
    try:
        split_conversation(session, CHAT, conv_id, plan)
        assert False, "应抛 ValueError"
    except ValueError as e:
        assert "多个组" in str(e)
    session.close()


def test_split_keep_root_must_match():
    _, session, _ = _setup()
    conv_id = _merged_mega(session)
    plan = _plan()
    plan[1]["root_message_id"] = "b2"  # 不是当前对话根 b1
    try:
        split_conversation(session, CHAT, conv_id, plan)
        assert False, "应抛 ValueError"
    except ValueError as e:
        assert "keep" in str(e)
    session.close()
