"""人工复核闭环测试:队列 / 定标回写 / 别名反哺 / gold set。

锁住的核心不是「能改标签」,而是**改完之后系统有没有变好**:
1. 队列只收该收的(未分类 + 低置信),人工确认过的不再反复塞回;
2. 定标必须同时写三处(messages.topic / topic_conf / topic_source /
   conversations.topic),否则页面、指标、gold set 各说各话;
3. 别名反哺要防毒化:**原标签是标准标签时绝不建别名**;
4. gold set 只认 topic_source='human',评估走与线上同一条判别路径。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Conversation, Message, TopicAlias
from app.models.topic_group import TopicGroup
from app.services.topic_review import (
    apply_review,
    gold_stats,
    list_review_queue,
)

CHAT = "oc_review"
T0 = 1700000000000
STANDARDS = ["屏幕闪烁", "画面模糊类问题", "遥控器失灵", "投屏搜不到设备"]


def _setup(with_standards=True):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    if with_standards:
        session.add(TopicGroup(name="全部", topics=list(STANDARDS), sort_order=0))
        session.commit()
    return session


def _group(session, root, texts, topic="", conf=0.0, source=""):
    """播种一个回复链组(messages + conversation 行,模拟真实落库状态)。"""
    for i, text in enumerate(texts):
        session.add(
            Message(
                message_id=f"{root}-{i}",
                chat_id=CHAT,
                sender_id=f"u{i}",
                sender_type="user",
                message_type="text",
                text=text,
                create_time=str(T0 + i * 60_000),
                root_message_id=root,
                topic=topic,
                topic_conf=conf,
                topic_source=source,
            )
        )
    session.add(
        Conversation(
            chat_id=CHAT,
            root_message_id=root,
            topic=topic,
            message_count=len(texts),
        )
    )
    session.commit()


# ---------------------------------------------------------------- 队列


def test_queue_contains_unclassified_and_low_conf_only():
    s = _setup()
    _group(s, "g1", ["电视一直闪"])                                   # 未分类
    _group(s, "g2", ["遥控器没反应"], "遥控器失灵", 0.3, "llm")         # 低置信
    _group(s, "g3", ["屏幕闪个不停"], "屏幕闪烁", 0.92, "llm")          # 高置信,不入队
    _group(s, "g4", ["画面糊了"], "画面模糊类问题", 1.0, "human")       # 人工已确认

    q = list_review_queue(s, low_conf=0.5)
    roots = [i["root_message_id"] for i in q["items"]]
    assert roots == ["g1", "g2"], "未分类优先,低置信次之;高置信与人工确认不入队"
    assert q["total"] == 2
    reasons = {i["root_message_id"]: i["reason"] for i in q["items"]}
    assert reasons == {"g1": "unclassified", "g2": "low_conf"}


def test_queue_carries_samples_for_human_judgement():
    s = _setup()
    _group(s, "g1", ["电视一直闪", "换个片源试试", "还是一样"])
    q = list_review_queue(s)
    item = q["items"][0]
    assert item["message_count"] == 3
    assert item["samples"], "必须带样本文本,否则人工没法判断"
    assert item["samples"][0] == "电视一直闪"
    assert len(item["samples"]) <= 2, "每组只取少量样例,不要整组铺开"


def test_queue_respects_chat_filter():
    s = _setup()
    _group(s, "g1", ["电视一直闪"])
    s.add(
        Message(
            message_id="o1",
            chat_id="other",
            sender_id="u1",
            sender_type="user",
            message_type="text",
            text="别的群",
            create_time=str(T0),
            root_message_id="o1",
        )
    )
    s.commit()
    q = list_review_queue(s, chat_id=CHAT)
    assert [i["root_message_id"] for i in q["items"]] == ["g1"]


# ---------------------------------------------------------------- 定标回写


def test_apply_review_writes_all_three_places():
    s = _setup()
    _group(s, "g1", ["电视一直闪"], "屏幕闪烁", 0.2, "llm")

    r = apply_review(s, CHAT, "g1", "屏幕闪烁")
    assert r["ok"] is True
    assert r["old_topic"] == "屏幕闪烁"

    rows = s.query(Message).filter_by(root_message_id="g1").all()
    assert {m.topic for m in rows} == {"屏幕闪烁"}
    assert {m.topic_conf for m in rows} == {1.0}, "人工确认视为满置信"
    assert {m.topic_source for m in rows} == {"human"}, "进入 gold set"
    conv = s.query(Conversation).filter_by(root_message_id="g1").one()
    assert conv.topic == "屏幕闪烁", "conversations 是页面展示副本,必须同步"


def test_apply_review_normalizes_human_typo_to_standard():
    """人工写法「画面模糊」应收敛到标准名,而不是造一个新标签。"""
    s = _setup()
    _group(s, "g1", ["画面糊了看不清"])
    r = apply_review(s, CHAT, "g1", "画面模糊")
    assert r["ok"] is True
    assert r["label"] == "画面模糊类问题"


def test_apply_review_rejects_non_standard_without_allow_new():
    s = _setup()
    _group(s, "g1", ["画面糊了看不清"])
    r = apply_review(s, CHAT, "g1", "玄学故障")
    assert r["ok"] is False
    assert "不在标准标签集内" in r["reason"]
    rows = s.query(Message).filter_by(root_message_id="g1").all()
    assert {m.topic for m in rows} == {""}, "被拒时库必须原样不动"
    assert {m.topic_source for m in rows} == {""}


def test_apply_review_allow_new_accepts_outside_label():
    s = _setup()
    _group(s, "g1", ["机器半夜自己唱歌"])
    r = apply_review(s, CHAT, "g1", "半夜自动播放", allow_new=True)
    assert r["ok"] is True
    assert r["label"] == "半夜自动播放"
    assert s.query(Message).filter_by(root_message_id="g1").first().topic == "半夜自动播放"


def test_apply_review_rejects_empty_label():
    s = _setup()
    _group(s, "g1", ["随便"])
    assert apply_review(s, CHAT, "g1", "  ")["ok"] is False


def test_apply_review_missing_group():
    s = _setup()
    assert apply_review(s, CHAT, "nope", "屏幕闪烁")["ok"] is False


# ---------------------------------------------------------------- 别名反哺


def test_apply_review_learns_alias_from_non_standard_label():
    s = _setup()
    _group(s, "g1", ["画面糊了"], "画质不清晰", 0.4, "llm")

    r = apply_review(s, CHAT, "g1", "画面模糊类问题")
    assert r["ok"] is True
    assert r["aliases_written"] == ["画质不清晰 -> 画面模糊类问题"]

    alias = s.query(TopicAlias).filter_by(alias="画质不清晰").one()
    assert alias.canonical == "画面模糊类问题"
    assert alias.auto == 0, "人工确认的别名不是自动建议"


def test_apply_review_never_aliases_between_standard_labels():
    """硬护栏:标准标签之间建别名会连锁改写整个体系,必须拒绝。"""
    s = _setup()
    _group(s, "g1", ["画面糊了"], "屏幕闪烁", 0.6, "llm")

    r = apply_review(s, CHAT, "g1", "画面模糊类问题")
    assert r["ok"] is True
    assert r["aliases_written"] == []
    assert "标准标签" in r["alias_skipped"]
    assert s.query(TopicAlias).count() == 0


def test_apply_review_keeps_existing_manual_alias():
    s = _setup()
    s.add(TopicAlias(alias="画质不清晰", canonical="屏幕闪烁", auto=0))
    s.commit()
    _group(s, "g1", ["画面糊了"], "画质不清晰", 0.4, "llm")

    r = apply_review(s, CHAT, "g1", "画面模糊类问题")
    assert r["ok"] is True
    assert r["aliases_written"] == [], "人工维护的别名不被覆盖"
    assert s.query(TopicAlias).filter_by(alias="画质不清晰").one().canonical == "屏幕闪烁"


def test_apply_review_overrides_auto_alias():
    """自动建议的别名本来就在等人工裁决,人工结论应覆盖它。"""
    s = _setup()
    s.add(TopicAlias(alias="画质不清晰", canonical="屏幕闪烁", auto=1))
    s.commit()
    _group(s, "g1", ["画面糊了"], "画质不清晰", 0.4, "llm")

    r = apply_review(s, CHAT, "g1", "画面模糊类问题")
    assert r["aliases_written"] == ["画质不清晰 -> 画面模糊类问题"]
    row = s.query(TopicAlias).filter_by(alias="画质不清晰").one()
    assert row.canonical == "画面模糊类问题"
    assert row.auto == 0


def test_apply_review_idempotent():
    s = _setup()
    _group(s, "g1", ["画面糊了"], "画质不清晰", 0.4, "llm")
    first = apply_review(s, CHAT, "g1", "画面模糊类问题")
    second = apply_review(s, CHAT, "g1", "画面模糊类问题")
    assert first["aliases_written"] and second["aliases_written"] == []
    assert second["alias_skipped"], "第二次应说明「无错可学」"
    assert s.query(TopicAlias).count() == 1


def test_apply_review_propagates_same_wrong_label():
    """人工纠正一条后,库里同样错标的组应能一并回刷。"""
    s = _setup()
    _group(s, "g1", ["画面糊了"], "画质不清晰", 0.4, "llm")
    _group(s, "g2", ["字幕重影"], "画质不清晰", 0.4, "llm")

    r = apply_review(s, CHAT, "g1", "画面模糊类问题", propagate=True)
    assert r["ok"] is True
    assert r["propagated_messages"] == 1, "本组已单独改过,只连带 g2 的 1 条"
    assert {
        m.topic for m in s.query(Message).filter_by(root_message_id="g2").all()
    } == {"画面模糊类问题"}
    # 回刷不伪造 gold:g2 的来源保持原样
    assert {
        m.topic_source for m in s.query(Message).filter_by(root_message_id="g2").all()
    } == {"llm"}


# ---------------------------------------------------------------- gold set


def test_gold_stats_counts_only_human_confirmed():
    s = _setup()
    _group(s, "g1", ["电视一直闪"], "屏幕闪烁", 0.9, "llm")
    _group(s, "g2", ["一下闪一下黑"], "屏幕闪烁", 1.0, "human")
    _group(s, "g3", ["遥控器没反应"], "遥控器失灵", 1.0, "human")

    stats = gold_stats(s)
    assert stats["gold_groups"] == 2
    assert stats["total_groups"] == 3
    assert stats["human_rate"] == round(2 / 3, 4)
    assert stats["labels"] == {"屏幕闪烁": 1, "遥控器失灵": 1}


def test_apply_review_makes_group_leave_the_queue():
    """闭环的验收标准:人工处理过的组不能再次出现在队列里。"""
    s = _setup()
    _group(s, "g1", ["电视一直闪"])
    assert len(list_review_queue(s)["items"]) == 1

    apply_review(s, CHAT, "g1", "屏幕闪烁")
    assert list_review_queue(s)["items"] == []
    assert gold_stats(s)["gold_groups"] == 1


def test_apply_review_dry_run_writes_nothing():
    """预览与真跑走同一套校验,但一行都不留——否则预览就是在赌。"""
    s = _setup()
    _group(s, "g1", ["画面糊了"], "画质不清晰", 0.4, "llm")

    r = apply_review(s, CHAT, "g1", "画面模糊类问题", dry_run=True, propagate=True)
    assert r["ok"] is True and r["dry_run"] is True
    assert r["aliases_written"], "预览要能看出会学哪条别名"
    # 库必须原样
    assert {
        m.topic for m in s.query(Message).filter_by(root_message_id="g1").all()
    } == {"画质不清晰"}
    assert s.query(TopicAlias).count() == 0
    assert s.query(Message).first().topic_source == "llm"


# ---------------------------------------------------------------- HTTP 层


def test_review_queue_endpoint():
    from app.api.routes import api_review_queue

    s = _setup()
    _group(s, "g1", ["电视一直闪"])
    r = api_review_queue(chat_id=CHAT, limit=10, low_conf=0.5, db=s)
    assert r["total"] == 1
    assert r["items"][0]["root_message_id"] == "g1"


def test_review_apply_endpoint_and_error_status():
    import pytest
    from fastapi import HTTPException

    from app.api.routes import ReviewApplyIn, api_review_apply

    s = _setup()
    _group(s, "g1", ["电视一直闪"])
    ok = api_review_apply(
        ReviewApplyIn(chat_id=CHAT, root_message_id="g1", label="屏幕闪烁"),
        db=s,
        user=None,
    )
    assert ok["ok"] is True and ok["label"] == "屏幕闪烁"

    _group(s, "g2", ["画面糊了"])
    with pytest.raises(HTTPException) as e:
        api_review_apply(
            ReviewApplyIn(chat_id=CHAT, root_message_id="g2", label="玄学故障"),
            db=s,
            user=None,
        )
    assert e.value.status_code == 400, "手滑写错标签应是 400 而不是静默入库"


def test_alias_api_refuses_standard_label_as_alias():
    """别名接口是毒化标签体系的第二个入口,同样要拦。"""
    import pytest
    from fastapi import HTTPException

    from app.api.routes import TopicAliasIn, api_topic_aliases_upsert

    s = _setup()
    with pytest.raises(HTTPException) as e:
        api_topic_aliases_upsert(
            TopicAliasIn(alias="屏幕闪烁", canonical="画面模糊类问题"), db=s, user=None
        )
    assert e.value.status_code == 400
    assert "标准标签" in e.value.detail
    assert s.query(TopicAlias).count() == 0


def test_metrics_exposes_review_block():
    from app.api.routes import api_topic_metrics

    s = _setup()
    _group(s, "g1", ["电视一直闪"])
    _group(s, "g2", ["遥控器没反应"], "遥控器失灵", 1.0, "human")
    r = api_topic_metrics(db=s)
    assert r["review"]["pending_groups"] == 1
    assert r["review"]["gold_groups"] == 1
    assert r["review"]["human_rate"] == 0.5


# ---------------------------------------------------------------- 页面入口
def test_review_page_assets_exist():
    """页面缺失时 /review 直接 500,而且这版前端是这套闭环的唯一入口。

    锁三件事:三个静态文件都在、页面引用的资源确实存在、导航里有链接——
    缺任何一个,人工闭环在界面上就等于不存在(只剩 curl)。
    """
    from app.api.routes import STATIC_DIR

    for name in ("review.html", "review.js", "review.css"):
        assert (STATIC_DIR / name).is_file(), f"缺少 app/static/{name}"

    html = (STATIC_DIR / "review.html").read_text(encoding="utf-8")
    # 页面引用的每个静态资源都必须真实存在(改名/删文件不会只挂一半)
    import re

    for ref in re.findall(r'/static/([\w.\-]+)', html):
        assert (STATIC_DIR / ref).is_file(), f"review.html 引用了不存在的 {ref}"

    nav = (STATIC_DIR / "nav.js").read_text(encoding="utf-8")
    assert "'/review'" in nav, "导航里没有 /review 入口,页面等于藏起来了"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
