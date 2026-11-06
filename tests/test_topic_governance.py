"""标签治理三项的测试:组内空标签回填 / 相似度兜底 / 未分类清单。

对应三个已确认的存量问题:
1. 已打标组后来到达的消息永远 topic=""(旧调度见「组已有标签」直接 return);
2. LLM 输出的 near-miss(「画面模糊」vs「画面模糊类问题」)被闭集判死成未分类;
3. 未分类组在页面上完全不可见(list_topics 只查 topic != '')。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.dao.message_repo import MessageRepo
from app.models import Base, Conversation, Message, TopicGroup
from app.services.label_norm import normalize_label
from app.services.topic_service import TopicService, get_standard_labels

CHAT = "oc_gov"
T0 = 1700000000000


class FakeLLM:
    """记录调用次数,按队列返回;用于断言「已打标组不再调 LLM」。"""

    def __init__(self, *labels: str):
        self.labels = list(labels)
        self.calls: list[str] = []

    def chat(self, system: str, user: str) -> str:
        self.calls.append(user)
        if len(self.labels) > 1:
            return self.labels.pop(0)
        return self.labels[0]


def _session_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _add_group(Session, root: str, rows: list[tuple[str, str]], conv_topic: str = ""):
    """rows = [(message_id, topic)],建组 + 对话行。"""
    with Session() as s:
        for i, (mid, topic) in enumerate(rows):
            s.add(
                Message(
                    message_id=mid,
                    chat_id=CHAT,
                    sender_id="u1",
                    message_type="text",
                    text=f"消息{mid}",
                    create_time=str(T0 + i * 1000),
                    root_message_id=root,
                    conversation_id=1,
                    topic=topic,
                )
            )
        s.add(
            Conversation(
                id=1,
                chat_id=CHAT,
                root_message_id=root,
                root_sender_id="u1",
                topic=conv_topic,
            )
        )
        s.commit()


# ------------------------------------------------------- 1. 组内空标签回填


def test_backfill_fills_new_message_topic():
    Session = _session_factory()
    _add_group(Session, "r1", [("m1", "投屏卡顿"), ("m2", "投屏卡顿")])
    # 组已打标之后又来了两条(topic 为空)
    with Session() as s:
        for mid in ("m3", "m4"):
            s.add(
                Message(
                    message_id=mid,
                    chat_id=CHAT,
                    sender_id="u1",
                    message_type="text",
                    text="后续消息",
                    create_time=str(T0 + 5000),
                    root_message_id="r1",
                    conversation_id=1,
                    topic="",
                )
            )
        s.commit()

    svc = TopicService(session_factory=Session, llm=FakeLLM("不该被调用"))
    handled = svc.schedule_group(CHAT, "r1")
    assert handled is False, "已打标组不该再调度 LLM"
    assert svc._llm.calls == [], "不应调用 LLM"

    with Session() as s:
        topics = {
            m.message_id: m.topic
            for m in s.query(Message).filter(Message.chat_id == CHAT).all()
        }
    assert topics == {"m1": "投屏卡顿", "m2": "投屏卡顿", "m3": "投屏卡顿", "m4": "投屏卡顿"}


def test_backfill_syncs_conversation_topic():
    """conversations.topic 为空但消息有标签(审计 19 组)要一并补齐。"""
    Session = _session_factory()
    _add_group(Session, "r2", [("n1", "画面模糊")], conv_topic="")
    svc = TopicService(session_factory=Session, llm=FakeLLM("不该被调用"))
    assert svc.backfill_group_topic(CHAT, "r2") is True
    with Session() as s:
        conv = s.query(Conversation).filter_by(root_message_id="r2").one()
    assert conv.topic == "画面模糊"


def test_backfill_reports_unlabeled_group_as_unhandled():
    """还没标签的组必须继续走正常分类流程(不能把 return False 弄反)。"""
    Session = _session_factory()
    _add_group(Session, "r3", [("p1", "")])
    svc = TopicService(session_factory=Session, llm=FakeLLM("x"))
    assert svc.backfill_group_topic(CHAT, "r3") is False


# ------------------------------------------------------- 2. 相似度兜底


def test_similarity_rescues_near_miss():
    """少写/多写后缀的 near-miss 应被兜底救回标准标签,而不是判死成未分类。"""
    Session = _session_factory()
    standards = ["画面模糊类问题", "投屏卡顿", "语音唤不醒"]
    with Session() as s:
        got = normalize_label(
            s, "画面模糊", standards=standards, known_labels=[], sim_threshold=0.65
        )
    assert got == "画面模糊类问题"


def test_similarity_disabled_keeps_raw():
    Session = _session_factory()
    standards = ["画面模糊类问题"]
    with Session() as s:
        got = normalize_label(
            s, "画面模糊", standards=standards, known_labels=[], sim_threshold=0.0
        )
    assert got == "画面模糊", "阈值 0 时应保持原样(回到只认精确匹配)"


def test_similarity_does_not_over_merge():
    """不相干的标签不该被硬塞进某个标准标签(闭集校验前的最后一道)。"""
    Session = _session_factory()
    standards = ["画面模糊类问题", "投屏卡顿"]
    with Session() as s:
        got = normalize_label(
            s, "遥控器失灵", standards=standards, known_labels=[], sim_threshold=0.65
        )
    assert got == "遥控器失灵"


# ------------------------------------------------------- 3. 未分类清单


def _seed_mixed(session):
    """三组:全空 / 部分有标签 / 全有标签。"""
    rows = [
        ("a1", "g1", ""),
        ("a2", "g1", ""),
        ("b1", "g2", "投屏卡顿"),
        ("b2", "g2", ""),
        ("c1", "g3", "画面模糊"),
        ("c2", "g3", "画面模糊"),
    ]
    for i, (mid, root, topic) in enumerate(rows):
        session.add(
            Message(
                message_id=mid,
                chat_id=CHAT,
                sender_id="u1",
                message_type="text",
                text=f"消息{mid}",
                create_time=str(T0 + i * 1000),
                root_message_id=root,
                conversation_id=1,
                topic=topic,
            )
        )
    session.commit()


def test_unclassified_only_fully_empty_groups():
    Session = _session_factory()
    with Session() as s:
        _seed_mixed(s)
        rows = MessageRepo(s).list_unclassified_roots(CHAT)
        total_unclassified = MessageRepo(s).count_unclassified_roots(CHAT)
    assert [r["root_message_id"] for r in rows] == ["g1"], "部分有标签的组不算未分类"
    assert rows[0]["message_count"] == 2
    assert total_unclassified == 1


def test_unclassified_empty_when_all_labeled():
    Session = _session_factory()
    with Session() as s:
        s.add(
            Message(
                message_id="z1",
                chat_id=CHAT,
                sender_id="u1",
                message_type="text",
                text="只有已归类的组",
                create_time=str(T0),
                root_message_id="z1",
                conversation_id=1,
                topic="投屏卡顿",
            )
        )
        s.commit()
        assert MessageRepo(s).list_unclassified_roots(CHAT) == []
        assert MessageRepo(s).count_unclassified_roots(CHAT) == 0


# ------------------------------------------------------- 4. 标准集读取


def test_standard_labels_from_topic_groups():
    Session = _session_factory()
    with Session() as s:
        s.add(TopicGroup(name="画质类问题", topics=["画面模糊类问题"], sort_order=0))
        s.add(TopicGroup(name="语音类问题", topics=["语音唤不醒"], sort_order=1))
        s.commit()
        assert set(get_standard_labels(s)) == {"画面模糊类问题", "语音唤不醒"}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
