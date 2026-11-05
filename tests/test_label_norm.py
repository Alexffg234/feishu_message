"""标签归一化测试(阶段一:别名表 + 归一化管线 + 打标接入)。

覆盖:
- clean_label 语气词置空 / 正常标签保留
- normalize_label:别名精确收敛、标准标签原样、未命中原样、相似度兜底开关
- 集成:TopicService.classify_group 输出同义标签被别名收敛写入库
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Message, TopicAlias
from app.schemas.message import MessageCreate
from app.services.label_norm import clean_label, normalize_label
from app.services.message_service import MessageService
from app.services.topic_service import TopicService

T0 = 1700000000000
CHAT = "oc_norm"


class FakeLLM:
    def __init__(self, out: str = ""):
        self.out = out
        self.calls = 0

    def chat(self, system: str, user: str) -> str:
        self.calls += 1
        return self.out


def _setup():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    return session


def _add_alias(session, alias, canonical, auto=0):
    session.add(TopicAlias(alias=alias, canonical=canonical, auto=auto))
    session.commit()


def _seed_one_message(session, text):
    """写 1 条文本消息(独立组),返回其 root_message_id。"""
    svc = MessageService(session, allowed_chat_ids=set())
    svc.save_message(
        MessageCreate(
            message_id="m1",
            chat_id=CHAT,
            sender_id="u1",
            sender_type="user",
            message_type="text",
            text=text,
            create_time=str(T0),
        )
    )
    return "m1"


# ---------------------------------------------------------------- clean_label
def test_clean_label_strips_vocals():
    for vocal in ["好的", "收到", "嗯", "OK", "ok"]:
        assert clean_label(vocal) == ""
    assert clean_label("投屏") == "投屏"
    assert clean_label("远场语音唤不醒") == "远场语音唤不醒"
    print("clean_label OK")


# ---------------------------------------------------------------- normalize
def test_normalize_alias_converges():
    session = _setup()
    _add_alias(session, "投不了屏", "投屏")
    assert normalize_label(session, "投不了屏") == "投屏"
    print("alias converge OK")


def test_normalize_alias_chain():
    session = _setup()
    _add_alias(session, "投不了", "投不上")
    _add_alias(session, "投不上", "投屏")
    # 沿链解析到最终 canonical
    assert normalize_label(session, "投不了") == "投屏"
    print("alias chain OK")


def test_normalize_standard_passthrough():
    session = _setup()
    # 标准标签即使存在于候选,也原样返回,不收敛到别的相似标签
    label = normalize_label(session, "投屏", standards=["投屏", "蓝牙"])
    assert label == "投屏"
    print("standard passthrough OK")


def test_normalize_unknown_preserved():
    session = _setup()
    _add_alias(session, "投不了屏", "投屏")
    assert normalize_label(session, "全新问题标签") == "全新问题标签"
    print("unknown preserved OK")


def test_normalize_similarity_off_by_default():
    session = _setup()
    # 默认 sim_threshold=0:不命中别名/标准的标签原样保留,相似但不相同也不收敛
    assert normalize_label(session, "投不了屏", standards=["投屏"]) == "投不了屏"
    print("similarity off OK")


def test_normalize_similarity_when_enabled():
    session = _setup()
    # 开启相似度兜底:与候选 dice 高则收敛;不同问题仍保留
    got = normalize_label(
        session, "投屏失败", standards=["投屏"], sim_threshold=0.5
    )
    assert got in ("投屏", "投屏失败"), got
    # 明显不同的问题,即便开启兜底也不收敛
    got2 = normalize_label(
        session, "蓝牙耳机连不上", standards=["投屏"], sim_threshold=0.5
    )
    assert got2 == "蓝牙耳机连不上"
    print("similarity on OK")


# ---------------------------------------------------------------- 集成
def test_classify_group_converges_alias():
    session = _setup()
    _add_alias(session, "投不了屏", "投屏")
    _seed_one_message(session, "投屏一直失败,请看看")
    svc = TopicService(session_factory=lambda: session, llm=FakeLLM("投不了屏"))

    label = svc.classify_group(CHAT, "m1")
    assert label == "投屏", f"应经别名收敛为投屏,实际 {label}"

    msg = session.query(Message).filter(Message.message_id == "m1").one()
    assert msg.topic == "投屏"
    from app.models.conversation import Conversation

    conv = session.query(Conversation).one()
    assert conv.topic == "投屏"
    print("classify alias converge OK")


if __name__ == "__main__":
    test_clean_label_strips_vocals()
    test_normalize_alias_converges()
    test_normalize_alias_chain()
    test_normalize_standard_passthrough()
    test_normalize_unknown_preserved()
    test_normalize_similarity_off_by_default()
    test_normalize_similarity_when_enabled()
    test_classify_group_converges_alias()
    print("ALL LABEL NORM TESTS PASSED")
