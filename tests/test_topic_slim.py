"""标准体系瘦身测试:方案不变量 + 落库/回滚的可逆性。

这套脚本改的是**标签体系本身**,错一次的影响是全局的(所有历史统计、
所有后续判别都跟着变)。所以锁三类东西:

1. 方案自身的不变量(手改 MERGES 时最容易埋的雷);
2. 落库语义:合并 / 清空 / 别名三条路径各自只做该做的事;
3. 可逆性:--restore 必须逐条还原,而不是「大概还原」。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.models import Base, Conversation, Message
from app.models.topic_alias import TopicAlias
from app.models.topic_group import TopicGroup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from slim_topics import (  # noqa: E402
    MERGES,
    NEW_GROUPS,
    _as_topics,
    apply_plan,
    collect_state,
    preflight,
    render_report,
    restore,
    validate_plan,
)

CHAT = "oc_slim"
T0 = 1700000000000

# 一个「压缩版」的旧体系:层级是反的、组内混动作词、同义未收敛 —— 三个要治的病各来一例
OLD_GROUPS = [
    ("画质类问题", "", []),
    ("黑屏类问题", "画质类问题", ["酷喵黑屏", "切换4K黑屏", "有声无图"]),
    (
        "画面模糊类问题",
        "画质类问题",
        ["画面模糊", "画质模糊", "画质推送", "三方应用播放视频有黑点"],
    ),
    ("投屏问题", "", ["投屏找不到设备", "镜像投屏卡顿", "投屏卡顿"]),
    ("硬件", "", ["屏幕"]),
]


def _setup():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)()


def _seed(session, root, texts, topic, conf=1.0):
    for i, t in enumerate(texts):
        session.add(
            Message(
                message_id=f"{root}-{i}",
                chat_id=CHAT,
                sender_id="u1",
                sender_type="user",
                message_type="text",
                text=t,
                create_time=str(T0 + i * 1000),
                root_message_id=root,
                topic=topic,
                topic_conf=conf,
            )
        )
    session.add(
        Conversation(chat_id=CHAT, root_message_id=root, topic=topic, message_count=len(texts))
    )


def _seed_old_world(session):
    # sort_order 递增:旧库就是这样排的,restore 按它还原顺序。全给 1 的话
    # SQLite 只能按插入序返回,顺序断言就变成在赌实现细节而不是在测 restore。
    for i, (name, parent, topics) in enumerate(OLD_GROUPS):
        session.add(TopicGroup(name=name, parent=parent, topics=topics, sort_order=i))
    _seed(session, "g_black1", ["电视一直黑屏", "只有声音"], "酷喵黑屏")
    _seed(session, "g_black2", ["开机黑屏"], "有声无图")
    _seed(session, "g_blur1", ["字幕看不清"], "画质模糊")
    _seed(session, "g_blur2", ["画面糊"], "画面模糊")
    _seed(session, "g_push", ["帮我把画质推送关掉"], "画质推送")
    _seed(session, "g_shot", ["手机投屏卡"], "镜像投屏卡顿")
    _seed(session, "g_shot2", ["投屏卡"], "投屏卡顿")
    _seed(session, "g_screen", ["屏幕"], "屏幕")
    session.commit()


# ---------------------------------------------------------------- 方案不变量
def test_plan_has_no_duplicate_or_suffixed_labels():
    plan = validate_plan()
    std = plan["standards"]
    assert len(std) == len(set(std))
    # 「画面模糊类问题」这类后缀是旧体系的病,新体系里不能再出现
    assert not [s for s in std if s.endswith(("类问题", "的问题", "的情况"))]
    assert len(std) == sum(len(t) for _n, t in NEW_GROUPS)


def test_plan_targets_must_be_in_new_standards():
    plan = validate_plan()
    for old, new in MERGES.items():
        if (new or "").strip():
            assert new in plan["group_of"], f"{old} 的目标 {new} 不在新标准集里"


def test_every_new_standard_has_a_source_label():
    """每个新标签都得有旧标签指向它,否则它是凭空出现的,没有历史依据。"""
    targets = {(v or "").strip() for v in MERGES.values()}
    for s in validate_plan()["standards"]:
        assert s in targets, f"{s} 没有任何旧标签映射过来"


def test_duplicate_label_raises():
    import slim_topics as m

    old = m.NEW_GROUPS
    try:
        m.NEW_GROUPS = [("A", ["x"]), ("B", ["x"])]
        with pytest.raises(ValueError):
            validate_plan()
    finally:
        m.NEW_GROUPS = old


def test_suffixed_target_raises():
    import slim_topics as m

    old = m.NEW_GROUPS
    try:
        m.NEW_GROUPS = [("A", ["画面模糊类问题"])]
        with pytest.raises(ValueError):
            validate_plan()
    finally:
        m.NEW_GROUPS = old


# ---------------------------------------------------------------- 解析
def test_as_topics_handles_raw_sql_string():
    """原生 SQL 取 JSONText 回来是字符串,逐字符迭代过一次(151 个标签的假象)。"""
    assert _as_topics('["画面模糊", "花屏"]') == ["画面模糊", "花屏"]
    assert _as_topics(["画面模糊"]) == ["画面模糊"]
    assert _as_topics(None) == []
    assert _as_topics("not json") == []


# ---------------------------------------------------------------- 落库
def test_apply_flattens_groups_into_two_levels(tmp_path):
    _engine, s = _setup()
    _seed_old_world(s)
    apply_plan(s, validate_plan(), tmp_path)

    rows = s.execute(text("SELECT name, parent FROM topic_groups ORDER BY sort_order")).all()
    assert len(rows) == len(NEW_GROUPS)
    # 压平:不再有子组,父组名不再充当分类
    assert all(r[1] == "" for r in rows)
    assert "黑屏类问题" not in [r[0] for r in rows]


def test_apply_merges_synonyms(tmp_path):
    _engine, s = _setup()
    _seed_old_world(s)
    apply_plan(s, validate_plan(), tmp_path)

    # 黑屏族 3 条消息 / 2 个组合 1、模糊族 2 合 1、投屏卡顿 2 合 1
    assert s.execute(text("SELECT COUNT(*) FROM messages WHERE topic='黑屏'")).scalar() == 3
    assert s.execute(text("SELECT COUNT(*) FROM messages WHERE topic='画面模糊'")).scalar() == 2
    assert s.execute(text("SELECT COUNT(*) FROM messages WHERE topic='投屏卡顿'")).scalar() == 2
    # conversations 是展示副本,必须同步
    assert s.execute(
        text("SELECT COUNT(*) FROM conversations WHERE topic='黑屏'")
    ).scalar() == 2
    assert s.execute(
        text("SELECT COUNT(*) FROM conversations WHERE topic='镜像投屏卡顿'")
    ).scalar() == 0


def test_apply_clears_action_labels_and_zeroes_conf(tmp_path):
    """动作词不是现象:清空回未分类队列,置信度一并归零(否则会以高置信出现在统计里)。"""
    _engine, s = _setup()
    _seed_old_world(s)
    apply_plan(s, validate_plan(), tmp_path)

    row = s.execute(
        text("SELECT topic, topic_conf FROM messages WHERE root_message_id='g_push'")
    ).first()
    assert row[0] == "" and float(row[1]) == 0.0
    # 「屏幕」太笼统,同样清空
    assert s.execute(
        text("SELECT COUNT(*) FROM messages WHERE topic='屏幕'")
    ).scalar() == 0


def test_apply_writes_aliases_but_not_identity(tmp_path):
    _engine, s = _setup()
    _seed_old_world(s)
    res = apply_plan(s, validate_plan(), tmp_path)

    aliases = {a.alias: a for a in s.query(TopicAlias).all()}
    assert aliases["画质模糊"].canonical == "画面模糊"
    assert aliases["镜像投屏卡顿"].canonical == "投屏卡顿"
    assert aliases["酷喵黑屏"].canonical == "黑屏"
    # auto=0:这是人工定下的映射,不能被「自动建议」覆盖
    assert all(a.auto == 0 for a in aliases.values())
    # 原名保留的标签不建自指别名(自指别名会让 normalize 白跑一趟)
    assert "画面模糊" not in aliases
    assert "投屏卡顿" not in aliases
    assert res["aliases"] == len(validate_plan()["aliases"])


def test_apply_is_idempotent(tmp_path):
    _engine, s = _setup()
    _seed_old_world(s)
    apply_plan(s, validate_plan(), tmp_path)
    before = s.execute(text("SELECT COUNT(*) FROM messages WHERE topic<>''")).scalar()
    apply_plan(s, validate_plan(), tmp_path)
    after = s.execute(text("SELECT COUNT(*) FROM messages WHERE topic<>''")).scalar()
    assert before == after
    assert s.execute(text("SELECT COUNT(*) FROM topic_groups")).scalar() == len(NEW_GROUPS)
    assert s.query(TopicAlias).count() == len(validate_plan()["aliases"])


# ---------------------------------------------------------------- 可逆
def test_restore_roundtrip(tmp_path):
    """合并是 N→1 的,还原必须靠逐条快照,不能靠映射反推。"""
    _engine, s = _setup()
    _seed_old_world(s)
    before = dict(
        s.execute(text("SELECT id, topic FROM messages WHERE topic<>''")).all()
    )
    conv_before = dict(
        s.execute(text("SELECT id, topic FROM conversations WHERE topic<>''")).all()
    )

    res = apply_plan(s, validate_plan(), tmp_path)
    assert s.execute(text("SELECT COUNT(*) FROM messages WHERE topic='酷喵黑屏'")).scalar() == 0

    restore(s, Path(res["backup"]))

    assert dict(s.execute(text("SELECT id, topic FROM messages WHERE topic<>''")).all()) == before
    assert dict(
        s.execute(text("SELECT id, topic FROM conversations WHERE topic<>''")).all()
    ) == conv_before
    # 顺序要按 sort_order 读:topic_groups.name 有唯一索引,不带 ORDER BY 的
    # SELECT name 会被 SQLite 当成覆盖索引扫描,返回的是索引序(近似字典序),
    # 跟插入序无关 —— 那样测的是索引实现,不是 restore。
    names = [
        r[0]
        for r in s.execute(
            text("SELECT name FROM topic_groups ORDER BY sort_order, id")
        ).all()
    ]
    assert names == [g[0] for g in OLD_GROUPS]
    # 本次写入的别名一并撤销,不留下来污染后续 normalize
    assert s.query(TopicAlias).count() == 0


# ---------------------------------------------------------------- preflight
def test_preflight_blocks_without_stage_column():
    """没有 stage 列时摘掉「已推」「上门维修」= 丢掉唯一的进度信号,必须阻断。

    这里手搓一张「旧版」messages 表(只有 topic),模拟「代码没部署/没重启」的
    生产库:直接用 Base.metadata 建表永远带 stage 列,测不出这条护栏。
    """
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE messages (id INTEGER PRIMARY KEY, topic VARCHAR(128))"))
    warnings, blockers = preflight(bind=engine)
    assert any("stage" in b for b in blockers)
    assert any("topic_conf" in w for w in warnings)


def test_preflight_passes_when_schema_current():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    # 测试用的 Base 里已带 stage/topic_conf/topic_source,等价于「已部署重启过」
    _warnings, blockers = preflight(bind=engine)
    assert blockers == []


# ---------------------------------------------------------------- 报表
def test_report_shows_counts_and_manual_handoff():
    _engine, s = _setup()
    _seed_old_world(s)
    out = render_report(collect_state(s), validate_plan())
    assert "标准标签 8 -> 33" in out or "-> 33" in out
    assert "清空 -> 未分类队列" in out
    assert "需要人工接手" in out


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            if "tmp_path" in fn.__code__.co_varnames[: fn.__code__.co_argcount]:
                import tempfile

                fn(Path(tempfile.mkdtemp()))
            else:
                fn()
            print(f"ok  {name}")
