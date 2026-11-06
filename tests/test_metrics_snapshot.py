"""标签体系改造前后的指标快照与对比测试。

这套东西的价值全在**判断方向对不对**:把「未分类率上升」判成退化,会让人以为
改造把系统搞坏了,然后去回滚一个正确的改动;把「归并率下降」判成退化,同理。
所以这里锁的不是「能生成报告」,而是:

1. 体系变更带来的**预期变化**不能被标成恶化(未分类率、待复核组);
2. **口径变化**的指标不能被当成效果(覆盖率、归并率、碎片数);
3. 持平的项不能瞎写解释(踩过:0 → 0 也输出「变小了」);
4. 「前」快照必须在写库前抓到,抓失败也不能阻断写库。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Conversation, Message
from app.models.topic_group import TopicGroup
from metrics_snapshot import (
    SCHEMA,
    capture,
    load,
    render_diff,
    save,
)
from slim_topics import apply_plan, validate_plan

CHAT = "oc_snap"
T0 = 1700000000000

# 一份最小旧体系:1 组同义(画质模糊 -> 画面模糊)+ 2 个动作词(已推 / 屏幕 -> 清空)
OLD_STANDARDS = ["画质模糊", "画面模糊", "已推", "屏幕"]


def _setup():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(TopicGroup(name="旧体系", parent="", topics=list(OLD_STANDARDS), sort_order=0))
    s.commit()
    return s


def _group(s, root, topic, texts=None):
    texts = texts or [f"{root} 的消息"]
    for i, t in enumerate(texts):
        s.add(
            Message(
                message_id=f"{root}-{i}",
                chat_id=CHAT,
                sender_id="u1",
                sender_type="user",
                message_type="text",
                text=t,
                create_time=str(T0 + i * 60_000),
                root_message_id=root,
                topic=topic,
                topic_conf=1.0 if topic else 0.0,
            )
        )
    s.add(
        Conversation(
            chat_id=CHAT, root_message_id=root, topic=topic, message_count=len(texts)
        )
    )


def _seed(s):
    """播种:同义两支 + 两个动作词标签,让「合并」和「清空」各来一例。"""
    _group(s, "g_blur1", "画质模糊", ["画面糊"])
    _group(s, "g_blur2", "画质模糊", ["字幕看不清"])
    _group(s, "g_blur3", "画面模糊", ["模糊"])
    _group(s, "g_push", "已推", ["已经推送了"])
    _group(s, "g_screen", "屏幕", ["屏幕"])
    s.commit()


def _snap(**over):
    """手搓一份快照,用于精确测判断逻辑 —— 不必每次都真跑去改库。"""
    base = {
        "schema": SCHEMA,
        "created_at": "2026-11-06T10:00:00",
        "label": "before",
        "standard_count": 41,
        "used_standard_count": 12,
        "topic_count": 62,
        "groups": 88,
        "standard_rate": 0.29,
        "off_standard_count": 29,
        "unclassified_groups": 10,
        "unclassified_rate": 0.11,
        "low_frequency_ratio": 0.42,
        "low_frequency_topic_count": 26,
        "merge_rate": 0.6,
        "remaining_fragmented": 30,
        "conf_high": 0,
        "conf_medium": 0,
        "conf_low": 0,
        "conf_none": 0,
        "pending_groups": 10,
        "gold_groups": 0,
        "labels": {"换机推送": 1, "已推": 1},
        "off_standard": {"酷喵黑屏": 2},
        "plan": {},
    }
    base.update(over)
    return base


# ---------------------------------------------------------------- 抓取 / 存取
def test_capture_has_comparable_fields_only():
    """三个进程内累计指标不能进快照 —— 它们重启归零,比出来的差值没有意义。"""
    s = _setup()
    _seed(s)
    d = capture(s, label="t")
    for key in ("standard_count", "topic_count", "unclassified_rate", "merge_rate",
                "off_standard_count", "labels", "gold_groups"):
        assert key in d, f"快照缺字段 {key}"
    for key in ("normalization", "recall", "stitching", "stitch_confirm_rate"):
        assert key not in d, f"进程内累计指标不该进快照:{key}"
    assert d["labels"]["画质模糊"] == 2


def test_save_and_load_roundtrip(tmp_path):
    s = _setup()
    _seed(s)
    path = save(s, "before", tmp_path)
    assert path.is_file()
    assert path.name.startswith("metrics-before-")
    d = load(path)
    assert d["label"] == "before"
    assert d["standard_count"] == 4
    # 存的是 UTF-8,中文标签不能变成转义序列(人要直接读这个文件)
    raw = path.read_text(encoding="utf-8")
    assert "画质模糊" in raw
    json.loads(raw)  # 必须是合法 JSON


def test_load_rejects_other_schema_version(tmp_path):
    p = tmp_path / "metrics-x.json"
    p.write_text(json.dumps({"schema": 99}), encoding="utf-8")
    try:
        load(p)
    except ValueError as e:
        assert "快照格式版本" in str(e)
    else:
        raise AssertionError("格式版本不对时必须报错,不能拿两份口径不同的快照硬比")


# ------------------------------------------------------- 判断方向(核心)
def test_expected_changes_are_not_reported_as_regression():
    """体系瘦身后未分类必升 —— 判成「恶化」会让正确改动被回滚。"""
    before = _snap()
    after = _snap(
        standard_count=33,
        topic_count=33,
        unclassified_groups=22,
        unclassified_rate=0.25,
        pending_groups=22,
        labels={"换机推送": 0},
        plan={"cleared_labels": ["换机推送", "已推"], "cleared_groups": 12},
    )
    text = render_diff(before, after)
    assert "预期" in text
    assert "✗ 恶化" not in text, "预期代价被误判成恶化"
    assert "真正变差" not in text


def test_merge_rate_is_not_comparable_when_standard_set_changes():
    """标签合并会让同一标签下聚集更多组,归并率天然下降 —— 不能当退化。

    这是最容易写错的一条:归并率看起来是「质量指标」,但它的分母是
    (群, 标签) 组合数,标签一合并分母就变了。
    """
    before = _snap(merge_rate=0.62, remaining_fragmented=30)
    after = _snap(
        standard_count=33,
        merge_rate=0.18,
        remaining_fragmented=55,
        plan={"cleared_labels": ["换机推送"], "cleared_groups": 1},
    )
    text = render_diff(before, after)
    line = next(l for l in text.splitlines() if "归并率" in l and "前" not in l)
    assert "不作退化判断" in line
    assert "✗ 恶化" not in text


def test_merge_rate_still_judged_when_standard_set_unchanged():
    """体系没变时归并率照常可比,否则这个指标就废了。"""
    before = _snap(merge_rate=0.5)
    after = _snap(merge_rate=0.8)  # standard_count 不变
    text = render_diff(before, after)
    line = next(l for l in text.splitlines() if "归并率" in l and "前" not in l)
    assert "✓ 改善" in line


def test_flat_rows_get_no_explanation():
    """持平就不该有解释。踩过的坑:早期 0 → 0 也输出「变小了 / 冒出来了」。"""
    a = _snap(off_standard_count=0, gold_groups=0, unclassified_rate=0.1,
              unclassified_groups=9, pending_groups=9, standard_count=33)
    b = dict(a, created_at="2026-11-06T11:00:00", label="after")
    text = render_diff(a, b)
    for bad in ("冒出来", "变小", "变多", "收敛了"):
        assert bad not in text, f"持平状态却写了「{bad}」"
    # 全持平也不该冒出「真正变差」
    assert "真正变差" not in text


def test_coverage_is_flagged_not_comparable_both_directions():
    """覆盖率的分母变了,说明必须跟着方向走 —— 写死「上升」会变成假话。"""
    # 分母变小(瘦身)
    t1 = render_diff(_snap(standard_count=41, standard_rate=0.3),
                     _snap(standard_count=33, standard_rate=0.5))
    assert "口径变化" in t1 and "自动上升" in t1
    # 分母变大(扩容)
    t2 = render_diff(_snap(standard_count=11, standard_rate=0.73),
                     _snap(standard_count=33, standard_rate=0.09))
    assert "口径变化" in t2 and "天然下降" in t2
    assert "自动上升" not in t2


def test_diff_lists_disappeared_and_new_labels():
    before = _snap(labels={"酷喵黑屏": 3, "已推": 1})
    after = _snap(standard_count=33, labels={"黑屏": 4})
    text = render_diff(before, after)
    assert "消失 2 个" in text
    assert "酷喵黑屏" in text
    assert "新增 1 个" in text
    assert "黑屏" in text


# ------------------------------------------------------- apply 集成
def test_apply_captures_before_and_after(tmp_path):
    """「前」只能在写库那一刻抓 —— 之后 messages.topic 已改写,再也拿不到。"""
    s = _setup()
    _seed(s)
    res = apply_plan(s, validate_plan(), tmp_path)

    assert res["metrics_before"], "没有抓 before 快照,前后对比将永久缺失"
    assert res["metrics_after"], "没有抓 after 快照"
    b = load(Path(res["metrics_before"]))
    a = load(Path(res["metrics_after"]))
    assert b["standard_count"] == 4       # 改造前
    assert a["standard_count"] == 33      # 改造后
    assert b["unclassified_groups"] == 0
    assert a["unclassified_groups"] >= 2  # 动作词回队列
    # 方案摘要要跟着快照走,否则对比解释不出「未分类为什么升」
    assert a["plan"]["cleared_labels"]


def test_backup_records_metrics_before_path(tmp_path):
    """回滚时要知道当时对比的基线在哪,不然事后翻不到。"""
    s = _setup()
    _seed(s)
    res = apply_plan(s, validate_plan(), tmp_path)
    payload = json.loads(Path(res["backup"]).read_text(encoding="utf-8"))
    assert payload["metrics_before"] == res["metrics_before"]
    assert Path(payload["metrics_before"]).is_file()


def test_apply_still_writes_when_capture_fails(tmp_path, monkeypatch, capsys):
    """抓不到基线不阻断写库,但必须明确报出来。

    理由:apply 的主职责是改体系;快照失败只让对比不可用,不能因此把已经
    确认过的改造挡住 —— 但也不能静默,否则事后会以为「改造没产生变化」。
    """
    import metrics_snapshot

    def boom(*_a, **_kw):
        raise RuntimeError("缺 topic_conf 列")

    monkeypatch.setattr(metrics_snapshot, "save", boom)

    s = _setup()
    _seed(s)
    res = apply_plan(s, validate_plan(), tmp_path)

    assert res["metrics_before"] == "" and res["metrics_after"] == ""
    out = capsys.readouterr().out
    assert "无法抓取" in out and "缺 topic_conf 列" in out
    # 写库本身必须照常完成
    assert res["aliases"] > 0
    from sqlalchemy import text

    from slim_topics import NEW_GROUPS, _as_topics

    # topic_groups 是「大类」表(瘦身后 11 行),标签数才是 33 —— 两者别搞混
    assert s.execute(text("SELECT COUNT(*) FROM topic_groups")).scalar() == len(NEW_GROUPS)
    flat = [t for _g in NEW_GROUPS for t in _g[1]]
    assert len(flat) == 33
    got = s.execute(text("SELECT topics FROM topic_groups")).scalars().all()
    assert sum(len(_as_topics(v)) for v in got) == 33
