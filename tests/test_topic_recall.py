"""候选召回测试:把「41 选 1」缩成「8 选 1」。

锁三件事:
1. 打分与召回顺序(精确命中 > 覆盖度 > 样例相似),候选永远是标准集子集;
2. **不该窄化的情形绝不窄化**(标签本来就少 / 关闭召回 / 全零信号);
3. 集成链路的**安全阀**:窄化后 LLM 判 UNKNOWN → 用全集重问一次,且只重问一次。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.models import Base, Message
from app.services import topic_recall
from app.services.topic_recall import (
    filter_tree,
    recall_standards,
    score_label,
)
from app.services.topic_service import TopicService

CHAT = "oc_recall"
T0 = 1700000000000

# 41 个标签的缩小版:足够触发窄化(top_k=8),含近义族与跨大类
STANDARDS = [
    "投屏搜不到设备",
    "投屏频繁断开",
    "屏幕闪烁",
    "画面模糊类问题",
    "画面偏色",
    "开机黑屏",
    "语音唤醒无响应",
    "语音识别不准",
    "遥控器失灵",
    "声音异常",
    "网络连接失败",
    "自动关机",
]


def _with_top_k(value: int):
    st = get_settings()
    old = st.topic_recall_top_k
    st.topic_recall_top_k = value
    return old


def _restore(seconds: int) -> None:
    get_settings().topic_recall_top_k = seconds


# ---------------------------------------------------------------- 打分 / 召回


def test_exact_hit_scores_one():
    assert score_label("屏幕闪烁", "电视一直屏幕闪烁,看着眼晕") == 1.0


def test_cover_beats_dice_on_long_text():
    """长文本下 Dice 被稀释到接近 0,覆盖度仍能给出可用的分数。

    「画面模糊类问题」7 个字里 3 个是「类问题」,不剥后缀只得 2/6=0.33,
    剥掉后 2/3=0.67——这是标签带类别后缀时必须做的一步。
    """
    text = "这台机器买回来第三天就发现画面有点模糊,换了片源还是一样,急死人了"
    assert score_label("画面模糊类问题", text) > 0.6
    assert score_label("遥控器失灵", text) == 0.0


def test_exemplar_recall_when_label_absent():
    """标签名完全不含字面时,靠该标签的历史消息也能召回。

    「声音异常」与「有杂音,沙沙的」没有任何共同 2-gram(覆盖度 0),
    但历史样例「喇叭有沙沙的杂音」与它高度相似 → 召回得到分数。
    """
    text = "有杂音,沙沙的"
    exemplars = {"声音异常": ["喇叭有沙沙的杂音"]}
    assert score_label("声音异常", text) == 0.0, "无字面覆盖"
    assert score_label("声音异常", text, exemplars["声音异常"]) > 0.4
    out = recall_standards(text, STANDARDS, exemplars, top_k=3)
    assert out[0] == "声音异常", "样例相似应把该标签召回并排第一"


def test_recall_returns_ordered_subset():
    out = recall_standards("投屏的时候搜不到设备", STANDARDS, top_k=8)
    assert len(out) == 8
    assert out[0] == "投屏搜不到设备", "精确命中排第一"
    assert set(out) <= set(STANDARDS), "候选必须是标准集子集"


def test_recall_keeps_standard_order_on_tie():
    """同分时保持标准集原顺序,结果确定可复现(不依赖 dict/set 顺序)。"""
    a = recall_standards("完全无关的一句话", ["甲甲", "乙乙", "丙丙", "丁丁"], top_k=2)
    b = recall_standards("完全无关的一句话", ["甲甲", "乙乙", "丙丙", "丁丁"], top_k=2)
    assert a == b


# ---------------------------------------------------------------- 不该窄化


def test_small_standard_set_not_narrowed():
    """标准集本身不超过 top_k:原样返回,不做无意义的裁剪。"""
    small = STANDARDS[:5]
    assert recall_standards("投屏搜不到设备", small, top_k=8) == small


def test_top_k_zero_disables_recall():
    assert recall_standards("投屏搜不到设备", STANDARDS, top_k=0) == STANDARDS


def test_no_signal_returns_full_set():
    """全零信号时窄化等于瞎猜,必须给全集。"""
    out = recall_standards("嗯嗯好的", STANDARDS, top_k=3)
    assert out == STANDARDS


def test_empty_standards():
    assert recall_standards("随便", [], top_k=8) == []


# ---------------------------------------------------------------- 树裁剪


def test_filter_tree_keeps_hierarchy_and_drops_empty_branch():
    tree = [
        {"name": "画面类", "topics": ["屏幕闪烁", "画面偏色"]},
        {"name": "语音类", "topics": [], "sub": [{"name": "唤醒", "topics": ["语音唤醒无响应"]}]},
        {"name": "遥控类", "topics": ["遥控器失灵"]},
    ]
    out = filter_tree(tree, {"屏幕闪烁"})
    assert out == [{"name": "画面类", "topics": ["屏幕闪烁"]}], "整类无候选的节点应摘掉"
    # 子类层级保留:大类名是语境,不能拍平
    out2 = filter_tree(tree, {"语音唤醒无响应"})
    assert out2 == [
        {"name": "语音类", "sub": [{"name": "唤醒", "topics": ["语音唤醒无响应"]}]}
    ]


# ---------------------------------------------------------------- 集成:安全阀


class _QueueLLM:
    """按队列返回预设输出;记录每次拿到的提示词,供断言「重问用的是全集」。"""

    def __init__(self, replies):
        self._replies = list(replies)
        self.prompts: list[str] = []

    def chat(self, system: str, user: str) -> str:
        self.prompts.append(user)
        if not self._replies:
            raise AssertionError("LLM 被调用了超出预期的次数")
        return self._replies.pop(0)


def _setup(llm):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(
        Message(
            message_id="r1",
            chat_id=CHAT,
            sender_id="u1",
            sender_type="user",
            message_type="text",
            text="电视一直屏幕闪烁,看着眼晕",
            create_time=str(T0),
            root_message_id="r1",
        )
    )
    session.commit()
    # 用同库的 session_factory 给 service,保证读到刚播种的数据
    return TopicService(session_factory=lambda: session, llm=llm), session


def _seed_standards(session, labels):
    """直接写 topic_groups,让 get_standard_labels 读得到闭集。"""
    from app.models.topic_group import TopicGroup

    session.add(TopicGroup(name="全部", topics=list(labels), sort_order=0))
    session.commit()


def test_unknown_on_candidates_retries_with_full_set():
    """窄化后判 UNKNOWN → 用全集重问一次,采第二次结果。"""
    old_topk = _with_top_k(8)
    old_sim = get_settings().topic_alias_sim_threshold
    get_settings().topic_alias_sim_threshold = 0
    topic_recall.reset_stats()
    try:
        llm = _QueueLLM(
            [
                '{"topic": "UNKNOWN", "confidence": 0.1}',
                '{"topic": "屏幕闪烁", "confidence": 0.9}',
            ]
        )
        svc, session = _setup(llm)
        _seed_standards(session, STANDARDS)
        label = svc.classify_group(CHAT, "r1", merge=False)

        assert label == "屏幕闪烁"
        assert len(llm.prompts) == 2, "应恰好重问一次"
        # 第一次提示词是候选子集,第二次是全量闭集
        first, second = llm.prompts
        assert "遥控器失灵" not in first, "召回应把它排除在候选外"
        assert "遥控器失灵" in second, "重问必须用全集"
        assert topic_recall.get_stats()["fallback"] == 1
    finally:
        _restore(old_topk)
        get_settings().topic_alias_sim_threshold = old_sim


def test_unknown_without_narrowing_does_not_retry():
    """没窄化(标准集本来就小)时判 UNKNOWN 不重问:同一道题问两遍没意义。"""
    old_topk = _with_top_k(8)
    old_sim = get_settings().topic_alias_sim_threshold
    get_settings().topic_alias_sim_threshold = 0
    topic_recall.reset_stats()
    try:
        llm = _QueueLLM(['{"topic": "UNKNOWN", "confidence": 0.1}'])
        svc, session = _setup(llm)
        _seed_standards(session, ["屏幕闪烁", "遥控器失灵"])
        label = svc.classify_group(CHAT, "r1", merge=False)

        assert label == "", "闭集外标签不入库,保留未分类"
        assert len(llm.prompts) == 1, "未窄化不该重问"
        assert topic_recall.get_stats()["fallback"] == 0
    finally:
        _restore(old_topk)
        get_settings().topic_alias_sim_threshold = old_sim


def test_recall_disabled_gives_full_set_prompt():
    """top_k=0 关闭召回:提示词给全集,且 UNKNOWN 不重问。"""
    old_topk = _with_top_k(0)
    old_sim = get_settings().topic_alias_sim_threshold
    get_settings().topic_alias_sim_threshold = 0
    topic_recall.reset_stats()
    try:
        llm = _QueueLLM(['{"topic": "UNKNOWN", "confidence": 0.1}'])
        svc, session = _setup(llm)
        _seed_standards(session, STANDARDS)
        label = svc.classify_group(CHAT, "r1", merge=False)

        assert label == ""
        assert len(llm.prompts) == 1
        assert "遥控器失灵" in llm.prompts[0], "关闭召回时提示词含全集"
        assert topic_recall.get_stats()["narrowed"] == 0
    finally:
        _restore(old_topk)
        get_settings().topic_alias_sim_threshold = old_sim


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
