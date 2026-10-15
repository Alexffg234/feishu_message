"""主题簇聚类(跨时间语义聚合)测试。

核心验证:交错的多主题对话(投屏/会议/固件)能被拆分为不同主题簇,
而不是被过度合并进 1 个簇。这是 L4 层的核心能力。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.message import Message
from app.services.cluster_service import (
    cluster_chat,
    _featurize,
    _jaccard,
    _char_bigrams,
    _clean_text,
    _is_noise,
    _weights,
    _dice_counter,
    _Cluster,
    to_text,
)
from tests.test_turn_merge import T0

_cid = "oc_cluster_test"
_id_counter = [0]


def _m(mid, sender, ts, text, mtype="text", chat_id=None):
    _id_counter[0] += 1
    return Message(
        id=_id_counter[0],
        message_id=mid,
        chat_id=chat_id or _cid,
        sender_id=sender,
        sender_type="user",
        message_type=mtype,
        text=text,
        create_time=str(ts),
        parent_message_id="",
        root_message_id="",
    )


# ---------------------------------------------------------------- 特征/噪音单测
def test_clean_text():
    assert "投屏" in _clean_text("投屏又失败了，你看下 https://x.com/a")
    assert "[链接]" not in _clean_text("投屏了")
    print("clean OK")


def test_is_noise():
    assert _is_noise("收到")
    assert _is_noise("好的")
    assert _is_noise("嗯")
    assert _is_noise("👍👍")
    assert not _is_noise("投屏又失败了")
    print("noise OK")


def test_featurize_shared_substring_should_low():
    """不同主题的消息,特征重叠应低(为聚类拆分提供依据)。"""
    a = _weights([_m("1", "u1", T0, "投屏失败重启一下")])
    b = _weights([_m("2", "u1", T0, "下午三点开会记得发通知")])
    s = _dice_counter(a, b)
    assert s < 0.12, f"异主题相似度应低,实际 {s:.2f}"
    print(f"cross-topic dice={s:.2f} (<0.12) OK")


def test_featurize_same_topic_should_high():
    """同一主题(投屏)的消息,特征重叠应显著 — 这是拆开交错话题的前提。"""
    a = _weights([_m("1", "u1", T0, "投屏又失败了重启一下")])
    b = _weights([_m("2", "u1", T0 + 10, "投屏失败我重启下试试")])
    s = _dice_counter(a, b)
    assert s > 0.2, f"同主题相似度应高,实际 {s:.2f}"
    print(f"same-topic dice={s:.2f} (>0.2) OK")


# ---------------------------------------------------------------- 交错聚类
def _interleaved_msgs():
    """3 条交织话题流(投屏/会议/固件),消息间隔 30s(远小于 600s 硬切)。

    注:真实群聊聊到某话题时会反复提及该关键词(投屏/固件),故每条线都带关键词,
    这是算法能拆开交错话题的内容依据;不带关键词的接话由"短回复就近接话"兜底。
    """
    rows = []
    t = T0
    # 投屏线
    rows.append(_m("a1", "u1", t, "投屏又失败了你们看看"))
    rows.append(_m("a2", "u3", t + 30_000, "我也遇到投屏失败的情况了"))
    # 会议线插进来
    rows.append(_m("b1", "u2", t + 60_000, "下午三点开会记得发通知给所有人"))
    rows.append(_m("b2", "u4", t + 90_000, "收到我一定提醒大家别迟到"))
    # 投屏线回来
    rows.append(_m("a3", "u3", t + 120_000, "投屏重启一下试试"))
    rows.append(_m("a4", "u1", t + 150_000, "投屏好了现在能用了"))
    # 固件线插进来
    rows.append(_m("c1", "u5", t + 180_000, "新固件升级后手感好多了"))
    rows.append(_m("c2", "u6", t + 210_000, "固件版本怎么查"))
    # 投屏线又回来收尾
    rows.append(_m("a5", "u3", t + 240_000, "投屏问题都解决了就行"))
    return rows


def test_interleaved_topics_split():
    rows = _interleaved_msgs()
    # 直接调用聚类(注入消息源),不依赖 DB
    import app.services.cluster_service as cs
    from app.dao.segment_repo import SegmentRepo
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.models import Base

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    for m in rows:
        session.add(m)
    session.commit()
    cs.SessionLocal = sessionmaker(bind=engine)

    clusters = cluster_chat(_cid)
    text = to_text(clusters)
    print(text)
    # 应拆出多个簇,且最大簇分属不同主题(投屏/会议/固件至少两类主题)
    assert len(clusters) >= 3, f"应交错拆出>=3簇,实际 {len(clusters)}"
    topics = [c["topic"] for c in sorted(clusters, key=lambda c: -c["message_count"])]
    print("top topics:", topics)
    assert any("固件" in t for t in topics), "应存在固件主题簇"
    assert any("投屏" in t for t in topics), "应存在投屏主题簇"
    # 核心:L4 跨段时间聚合 —— 被 会议/固件 交织打断的 投屏 线(5条)应收拢到同一簇
    screen = next(c for c in clusters if "投屏" in c["topic"])
    assert screen["message_count"] == 5, f"投屏线应交错聚合为1簇5条,实际 {screen['message_count']}条"
    msgs = {m["text"][:4] for m in screen["messages"]}
    assert msgs == {"投屏又失", "我也遇到", "投屏重启", "投屏好了", "投屏问题"}, f"投屏簇应含5条不同消息,实际 {msgs}"
    print("interleaved split OK")


def test_short_reply_chaining():
    """不带关键词的短接话(如"收到/我遇到了")应就近并入最近活跃簇,而非另起一簇。"""
    rows = [
        _m("s1", "u1", T0, "投屏卡死了怎么办", chat_id="oc_reply"),
        _m("s2", "u2", T0 + 30_000, "我这也遇到了", chat_id="oc_reply"),   # 短接话, 无共享关键词
        _m("s3", "u3", T0 + 60_000, "重启一下吧", chat_id="oc_reply"),
    ]
    import app.services.cluster_service as cs
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.models import Base

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    for m in rows:
        session.add(m)
    session.commit()
    cs.SessionLocal = sessionmaker(bind=engine)

    clusters = cluster_chat("oc_reply")
    print(to_text(clusters))
    # 3 条都应收拢到 1 个主题簇
    assert len(clusters) == 1, f"短接话应收拢为1簇,实际 {len(clusters)}"
    assert clusters[0]["message_count"] == 3
    print("short reply chaining OK")


if __name__ == "__main__":
    test_clean_text()
    test_is_noise()
    test_featurize_shared_substring_should_low()
    test_featurize_same_topic_should_high()
    test_interleaved_topics_split()
    test_short_reply_chaining()
    print("ALL CLUSTER TESTS PASSED")
