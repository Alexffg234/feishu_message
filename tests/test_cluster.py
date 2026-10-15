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


def _m(mid, sender, ts, text, mtype="text", chat_id=None, parent="", root="", raw=None):
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
        parent_message_id=parent,
        root_message_id=root,
        raw=raw,
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


def _run(rows, chat_id):
    """把消息注入内存 SQLite 并执行聚类,返回簇列表(供测试复用)。"""
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
    return cluster_chat(chat_id)


def test_interleaved_topics_split():
    clusters = _run(_interleaved_msgs(), _cid)
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


def test_noise_filler_attached():
    """现象A: 噪声回应节点(收到/好的)应收录进话题簇, 而非被丢弃。"""
    rows = [
        _m("n1", "u1", T0, "明天十点开会大家记得", chat_id="oc_filler"),
        _m("n2", "u2", T0 + 20_000, "收到", chat_id="oc_filler"),
        _m("n3", "u3", T0 + 40_000, "好的收到", chat_id="oc_filler"),
        _m("n4", "u4", T0 + 60_000, "没问题", chat_id="oc_filler"),
    ]
    clusters = _run(rows, "oc_filler")
    print(to_text(clusters))
    assert len(clusters) == 1, f"噪声回应应收录到唯一话题簇,实际 {len(clusters)}簇"
    c = clusters[0]
    assert c["message_count"] == 4, f"应收录4条(1主题+3回应),实际 {c['message_count']}"
    assert c["filler_count"] == 3, f"应标3条噪声回应,实际 {c['filler_count']}"
    assert c["content_count"] == 1
    filler_ids = {m["is_filler"] for m in c["messages"]}
    assert filler_ids == {True, False}, "簇内消息应同时含 主题发言(False) 与 回应节点(True)"
    print("noise filler attached OK")


def test_reply_chain_strong_merge():
    """现象C: 显式回复链(parent/root 已在某簇)即使内容不相似也强制并入同一话题。"""
    rows = [
        _m("r1", "u1", T0, "投影仪怎么连电脑", chat_id="oc_chain"),
        _m("r2", "u2", T0 + 30_000, "应该用HDMI线吧", chat_id="oc_chain", parent="r1", root="r1"),
        _m("r3", "u1", T0 + 60_000, "好的我先试试", chat_id="oc_chain", parent="r2", root="r1"),
    ]
    clusters = _run(rows, "oc_chain")
    print(to_text(clusters))
    # 内容上 r1 与 r2/r3 几无重叠, 但靠回复链强并入 → 应收拢为 1 簇
    assert len(clusters) == 1, f"回复链应收拢为1簇,实际 {len(clusters)}簇"
    assert clusters[0]["message_count"] == 3
    print("reply chain strong merge OK")


def test_nontext_evidence_attached():
    """现象B: 图片/文件消息应收录为话题的证据, 而非孤立成碎片。"""
    rows = [
        _m("e1", "u1", T0, "投屏驱动发群里了", chat_id="oc_evid"),
        _m("e2", "u1", T0 + 30_000, "", chat_id="oc_evid", mtype="file",
           raw={"file_name": "投屏驱动.zip", "mime_type": "application/zip", "file_key": "k1"}),
        _m("e3", "u2", T0 + 60_000, "收到我试试", chat_id="oc_evid"),
    ]
    clusters = _run(rows, "oc_evid")
    print(to_text(clusters))
    assert len(clusters) == 1, f"非文本证据应并入唯一话题簇,实际 {len(clusters)}簇"
    c = clusters[0]
    assert c["message_count"] == 3
    assert c["evidence"] and c["evidence"][0]["file_name"] == "投屏驱动.zip", \
        f"应收录文件证据,实际 {c['evidence']}"
    print("nontext evidence attached OK")


def test_short_reply_chaining():
    """不带关键词的短接话(如"收到/我遇到了")应就近并入最近活跃簇,而非另起一簇。"""
    rows = [
        _m("s1", "u1", T0, "投屏卡死了怎么办", chat_id="oc_reply"),
        _m("s2", "u2", T0 + 30_000, "我这也遇到了", chat_id="oc_reply"),   # 短接话, 无共享关键词
        _m("s3", "u3", T0 + 60_000, "重启一下吧", chat_id="oc_reply"),
    ]
    clusters = _run(rows, "oc_reply")
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
    test_noise_filler_attached()
    test_reply_chain_strong_merge()
    test_nontext_evidence_attached()
    test_short_reply_chaining()
    print("ALL CLUSTER TESTS PASSED")


if __name__ == "__main__":
    test_clean_text()
    test_is_noise()
    test_featurize_shared_substring_should_low()
    test_featurize_same_topic_should_high()
    test_interleaved_topics_split()
    test_short_reply_chaining()
    print("ALL CLUSTER TESTS PASSED")
