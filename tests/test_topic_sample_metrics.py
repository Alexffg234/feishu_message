"""阶段四:样本注入治理 + 指标埋点测试。

覆盖:
- _existing_topic_samples:从全量 50 收敛到默认 20;同群优先;与当前文本共现词优先排序
- _repr_rows:长链(>20)代表消息抽样——首条+末条+最长保留、含图片/文件消息、不硬截断
- label_norm 归一化收敛计数(get_norm_stats):别名命中 / 标准命中 / 无命中
- /api/topic_metrics 只读指标端点的返回结构
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Message, TopicAlias
from app.services.label_norm import _norm_stats, get_norm_stats, normalize_label
from app.services.topic_service import TopicService


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _add(
    session,
    mid,
    chat,
    topic,
    text,
    ts,
    mtype="text",
    root=None,
):
    session.add(
        Message(
            message_id=mid,
            chat_id=chat,
            sender_id="u",
            sender_type="user",
            message_type=mtype,
            text=text,
            create_time=str(ts),
            root_message_id=root or mid,
            topic=topic,
        )
    )


def _seed(engine, chat="oc"):
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    for i in range(40):
        _add(s, f"m{i}", chat, f"t{i % 25}", f"内容{i}", 1700000000000 + i)
    s.commit()
    return s


# ---------------------------------------------------------------- 样本治理
def test_samples_default_capped_at_20():
    s = _seed(create_engine("sqlite://"), "oc")
    from app.dao.message_repo import MessageRepo

    samples = TopicService._existing_topic_samples(MessageRepo(s), "oc")
    assert len(samples) <= 20, f"样本应收敛到 <=20,实际 {len(samples)}"
    print("sample cap OK")


def test_samples_prefer_same_chat_then_global():
    s = _seed(create_engine("sqlite://"), "oc")
    from app.dao.message_repo import MessageRepo

    # 同群只放了 25 个主题,足够 20 个,不会去全局补
    samples = TopicService._existing_topic_samples(MessageRepo(s), "oc")
    assert len(samples) == 20
    print("same-chat OK")


def test_samples_rank_cooccurrence_first():
    s = _session()
    _add(s, "m1", "oc", "投屏失败", "屏幕无法投屏", 1)
    _add(s, "m2", "oc", "蓝牙", "连接不稳定", 2)
    s.commit()
    from app.dao.message_repo import MessageRepo

    samples = TopicService._existing_topic_samples(
        MessageRepo(s), "oc", current_text="投屏有问题求助"
    )
    # 与当前文本共现字更多的「投屏失败」应排最前
    assert samples[0][0] == "投屏失败", samples
    print("co-occurrence rank OK")


def test_samples_no_current_text_keeps_recent_order():
    s = _session()
    _add(s, "m1", "oc", "旧主题", "很久以前", 1)
    _add(s, "m2", "oc", "新主题", "刚刚", 2)
    s.commit()
    from app.dao.message_repo import MessageRepo

    samples = TopicService._existing_topic_samples(MessageRepo(s), "oc")
    # 最近活跃优先:新主题(消息 id 更大、更晚)在前
    assert samples[0][0] == "新主题"
    print("recent order OK")


# ---------------------------------------------------------------- 长链代表消息抽样
def _mk_rows(session, n):
    rows = []
    for i in range(n):
        mtype = "image" if i == 5 else ("media" if i == 9 else "text")
        m = Message(
            message_id=f"r{i}",
            chat_id="oc",
            sender_id="u",
            message_type=mtype,
            text="长" * (i % 6 + 1),
            create_time=str(i),
            root_message_id="r0",
        )
        session.add(m)
        rows.append(m)
    session.commit()
    return rows


def test_repr_rows_samples_rather_than_truncate():
    s = _session()
    rows = _mk_rows(s, 30)
    sampled = TopicService._repr_rows(rows)  # limit 默认 20
    assert len(sampled) <= 20
    # 首条与末条必须保留(按 id 升序后首末仍在)
    assert sampled[0].message_id == "r0"
    assert sampled[-1].message_id == "r29"
    print("repr sample OK")


def test_repr_rows_includes_media_messages():
    s = _session()
    rows = _mk_rows(s, 25)
    sampled = TopicService._repr_rows(rows)
    types = {m.message_type for m in sampled}
    assert "image" in types and "media" in types, "应包含图片/视频消息元数据"
    print("repr media OK")


def test_repr_rows_short_chain_unchanged():
    s = _session()
    rows = _mk_rows(s, 5)
    sampled = TopicService._repr_rows(rows)
    assert len(sampled) == 5  # 未超 limit,原样全保留
    print("repr short OK")


# ---------------------------------------------------------------- 归一化计数
def test_norm_stats_counters():
    for k in _norm_stats:
        _norm_stats[k] = 0
    s = _session()
    s.add(TopicAlias(alias="投不了屏", canonical="投屏", auto=0))
    s.commit()

    assert normalize_label(s, "投不了屏") == "投屏"  # 别名命中
    assert normalize_label(s, "标准标签", standards=["标准标签"]) == "标准标签"  # 标准命中
    assert normalize_label(s, "未知标签") == "未知标签"  # 无命中

    st = get_norm_stats()
    assert st["total"] == 3
    assert st["alias_hit"] == 1
    assert st["standard_hit"] == 1
    assert st["similarity_hit"] == 0
    print("norm counters OK")


# ---------------------------------------------------------------- 指标端点
def test_topic_metrics_endpoint_shape():
    from app.api.routes import api_topic_metrics

    s = _session()
    _add(s, "a1", "oc", "投屏失败", "屏幕无法投屏", 1)
    _add(s, "b1", "oc", "投屏失败", "重启后依旧", 2)
    _add(s, "c1", "oc", "蓝牙", "连不上", 3)
    s.commit()

    r = api_topic_metrics(db=s)
    assert r["topic_count"] == 2
    assert r["low_frequency_topic_count"] == 1  # 蓝牙只 1 组
    assert r["merge_rate"] == 0.5  # 两个 (chat,topic):投屏失败已成单组,蓝牙单组
    assert "rate" in r["normalization"]
    assert "stitch_confirm_rate" in r
    assert "remaining_fragmented_topics" in r
    assert r["false_merge_sample"]["status"] == "needs_manual_review"
    print("metrics endpoint OK")


if __name__ == "__main__":
    test_samples_default_capped_at_20()
    test_samples_prefer_same_chat_then_global()
    test_samples_rank_cooccurrence_first()
    test_samples_no_current_text_keeps_recent_order()
    test_repr_rows_samples_rather_than_truncate()
    test_repr_rows_includes_media_messages()
    test_repr_rows_short_chain_unchanged()
    test_norm_stats_counters()
    test_topic_metrics_endpoint_shape()
    print("ALL TOPIC SAMPLE & METRICS TESTS PASSED")
