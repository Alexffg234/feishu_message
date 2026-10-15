"""标签归并集成测试(内存 SQLite + 假 LLM)。

验证:LLM 判定同群近义标签(亮度调节/读取开机方法/本控按键)并统一到保留标签,
应用映射后触发同标签归并;无映射/非法目标/成环/开关关闭时均安全无操作;
--relabel 的清空函数行为。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Conversation, Message
from app.schemas.message import MessageCreate
from app.services.label_merge import consolidate_labels
from app.services.message_service import MessageService

T0 = 1700000000000
CHAT = "oc_label"


class FakeLLM:
    def __init__(self, out: str = "{}"):
        self.out = out
        self.calls: list[str] = []

    def chat(self, system: str, user: str) -> str:
        self.calls.append(user)
        return self.out


def _setup():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    svc = MessageService(session, allowed_chat_ids=set())
    return svc, session


def _labeled_groups(svc, session):
    """三个单条消息的独立组,分别打 3 个近义标签(模拟 LLM 碎片化打标)。"""
    svc.save_message(_msg("a1", "u1", T0, "亮度无法调节,画面整体暗"))
    svc.save_message(_msg("b1", "u2", T0 + 120_000, "是不是本控按键卡主了"))
    svc.save_message(_msg("c1", "u3", T0 + 240_000, "去工厂菜单看看开机类型"))
    assert len(session.query(Conversation).all()) == 3
    from app.dao.conversation_repo import ConversationRepo
    from app.dao.message_repo import MessageRepo

    repo = MessageRepo(session)
    convo = ConversationRepo(session)
    for root, topic in [("a1", "亮度调节"), ("b1", "本控按键"), ("c1", "读取开机方法")]:
        repo.set_topic_for_group(CHAT, root, topic)
        convo.set_topic(CHAT, root, topic)
    session.commit()


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


def test_consolidate_merges_synonym_labels():
    svc, session = _setup()
    _labeled_groups(svc, session)

    llm = FakeLLM('{"亮度调节": "本控按键", "读取开机方法": "本控按键"}')
    applied = consolidate_labels(session, llm)

    assert sorted(applied) == [("亮度调节", "本控按键"), ("读取开机方法", "本控按键")]
    # 提示词带全部标签与样本
    assert "亮度调节" in llm.calls[0] and "本控按键" in llm.calls[0]

    # 标签统一 + 触发归并:3 组 → 1 对话,最早组 a1 为根
    convs = session.query(Conversation).all()
    assert len(convs) == 1, f"应归并为 1 个对话,实际 {len(convs)}"
    assert convs[0].root_message_id == "a1"
    assert convs[0].topic == "本控按键"
    assert convs[0].message_count == 3
    for m in session.query(Message).all():
        assert m.topic == "本控按键"
        assert m.root_message_id == "a1"
    print("consolidate merge OK")


def test_consolidate_noop_on_empty_json():
    svc, session = _setup()
    _labeled_groups(svc, session)

    llm = FakeLLM("{}")
    assert consolidate_labels(session, llm) == []
    assert len(session.query(Conversation).all()) == 3
    print("empty json OK")


def test_consolidate_ignores_invalid_targets():
    svc, session = _setup()
    _labeled_groups(svc, session)

    # 目标标签不在输入集 / 自映射 → 全部丢弃
    llm = FakeLLM('{"亮度调节": "新标签", "本控按键": "本控按键"}')
    assert consolidate_labels(session, llm) == []
    assert len(session.query(Conversation).all()) == 3
    print("invalid target OK")


def test_consolidate_cycle_dropped():
    svc, session = _setup()
    _labeled_groups(svc, session)

    llm = FakeLLM('{"亮度调节": "本控按键", "本控按键": "亮度调节"}')
    assert consolidate_labels(session, llm) == []
    assert len(session.query(Conversation).all()) == 3
    print("cycle OK")


def test_consolidate_transitive():
    svc, session = _setup()
    _labeled_groups(svc, session)

    # A→B 且 B→C → A 应直接到 C
    llm = FakeLLM('{"亮度调节": "读取开机方法", "读取开机方法": "本控按键"}')
    applied = consolidate_labels(session, llm)
    assert sorted(applied) == [("亮度调节", "本控按键"), ("读取开机方法", "本控按键")]
    assert len(session.query(Conversation).all()) == 1
    print("transitive OK")


def test_consolidate_disabled():
    from app.core.config import get_settings

    settings = get_settings()
    old = settings.topic_merge_enabled
    settings.topic_merge_enabled = False
    try:
        svc, session = _setup()
        _labeled_groups(svc, session)
        llm = FakeLLM('{"亮度调节": "本控按键"}')
        assert consolidate_labels(session, llm) == []
        assert len(session.query(Conversation).all()) == 3
    finally:
        settings.topic_merge_enabled = old
    print("disabled OK")


def test_relabel_clears_labels():
    from scripts.rethread_backfill import _clear_labels

    svc, session = _setup()
    _labeled_groups(svc, session)
    n = _clear_labels(session, None)
    assert n == 3
    assert all(m.topic == "" for m in session.query(Message).all())
    assert all(c.topic == "" for c in session.query(Conversation).all())
    print("relabel clear OK")


if __name__ == "__main__":
    test_consolidate_merges_synonym_labels()
    test_consolidate_noop_on_empty_json()
    test_consolidate_ignores_invalid_targets()
    test_consolidate_cycle_dropped()
    test_consolidate_transitive()
    test_consolidate_disabled()
    test_relabel_clears_labels()
    print("ALL LABEL MERGE TESTS PASSED")
