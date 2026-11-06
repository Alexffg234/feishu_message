"""问题单元切分测试(纯函数 + 内存 SQLite 集成)。

验证三件事:
1. 切点判据确定性:时间断档 / 设备号变化 / 显式块起点,边界值不误切;
2. 不切噪声:纯客套片并回上一片,图片片不被当噪声吸收;
3. 落地安全:计划精确覆盖、原对话根留 keep 组、新单元标签清空、
   跨切点的回复边不丢消息(父不在本对话时子消息按根渲染)、重复执行幂等。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.dao.conversation_repo import ConversationRepo
from app.models import Base, Conversation, Message
from app.schemas.message import MessageCreate
from app.services.case_split import (
    CUT_BLOCK,
    CUT_CASE_KEY,
    CUT_GAP,
    CUT_START,
    build_split_groups,
    plan_pieces,
    split_conversation_by_case,
)
from app.services.message_service import MessageService

T0 = 1700000000000
CHAT = "oc_case_split"
HOUR = 3600 * 1000


def _m(mid, ts, text="内容", sender="u1", **kw):
    """构造未入库的 Message 对象(纯函数测试用)。"""
    msg = Message(
        message_id=mid,
        chat_id=CHAT,
        sender_id=sender,
        message_type=kw.pop("message_type", "text"),
        text=text,
        create_time=str(ts),
    )
    for k, v in kw.items():
        setattr(msg, k, v)
    return msg


# ---------------------------------------------------------------- 纯函数:切点


def test_empty_and_single():
    assert plan_pieces([]) == []
    pieces = plan_pieces([_m("a", T0)])
    assert len(pieces) == 1 and pieces[0]["reason"] == CUT_START


def test_continuous_stays_one_piece():
    rows = [_m("a", T0), _m("b", T0 + 60_000), _m("c", T0 + 600_000)]
    assert len(plan_pieces(rows)) == 1


def test_gap_splits():
    rows = [_m("a", T0), _m("b", T0 + 30 * 60_000)]
    pieces = plan_pieces(rows)
    assert len(pieces) == 1, "半小时内不切"

    rows = [_m("a", T0), _m("b", T0 + 3 * HOUR)]
    pieces = plan_pieces(rows)
    assert len(pieces) == 2
    assert pieces[1]["reason"] == CUT_GAP
    assert [m.message_id for m in pieces[1]["rows"]] == ["b"]


def test_gap_boundary_is_strict():
    """恰好等于阈值不切(用 > 而非 >=),避免阈值边界抖动。"""
    pieces = plan_pieces([_m("a", T0), _m("b", T0 + 2 * HOUR)])
    assert len(pieces) == 1


def test_gap_configurable():
    rows = [_m("a", T0), _m("b", T0 + 40 * 60_000)]
    assert len(plan_pieces(rows)) == 1
    assert len(plan_pieces(rows, gap_sec=1800)) == 2


def test_case_key_change_splits_even_without_gap():
    rows = [
        _m("a", T0, case_key="sn:AAA111"),
        _m("b", T0 + 1000, case_key="sn:BBB222"),
    ]
    pieces = plan_pieces(rows)
    assert len(pieces) == 2
    assert pieces[1]["reason"] == CUT_CASE_KEY


def test_case_key_one_side_empty_does_not_split():
    """一侧没识别出设备号时无法确定换机,不能切(否则大量误切)。"""
    rows = [
        _m("a", T0, case_key="sn:AAA111"),
        _m("b", T0 + 1000, case_key=""),
        _m("c", T0 + 2000, case_key="sn:AAA111"),
    ]
    assert len(plan_pieces(rows)) == 1


def test_block_start_splits():
    rows = [_m("a", T0), _m("b", T0 + 1000, block_start=1)]
    pieces = plan_pieces(rows)
    assert len(pieces) == 2
    assert pieces[1]["reason"] == CUT_BLOCK


def test_block_start_on_first_message_is_not_a_cut():
    rows = [_m("a", T0, block_start=1), _m("b", T0 + 1000)]
    assert len(plan_pieces(rows)) == 1


def test_unsorted_input_is_sorted_by_time():
    rows = [_m("c", T0 + 2 * 60_000), _m("a", T0), _m("b", T0 + 60_000)]
    pieces = plan_pieces(rows)
    assert len(pieces) == 1
    assert [m.message_id for m in pieces[0]["rows"]] == ["a", "b", "c"]


def test_filler_piece_absorbed():
    """切出来的片只有「好的谢谢」→ 并回上一片,不单独成案。"""
    rows = [
        _m("a", T0),
        _m("b", T0 + 3 * HOUR, text="好的谢谢"),
        _m("c", T0 + 3 * HOUR + 60_000, text="收到"),
    ]
    pieces = plan_pieces(rows)
    assert len(pieces) == 1
    assert [m.message_id for m in pieces[0]["rows"]] == ["a", "b", "c"]


def test_mention_only_piece_absorbed():
    rows = [_m("a", T0), _m("b", T0 + 3 * HOUR, text="@_user_1 好的")]
    assert len(plan_pieces(rows)) == 1


def test_media_piece_not_absorbed():
    """图片消息文本为空但带信息,不能被当成客套片并走。"""
    rows = [_m("a", T0), _m("b", T0 + 3 * HOUR, text="", message_type="image")]
    pieces = plan_pieces(rows)
    assert len(pieces) == 2
    assert pieces[1]["rows"][0].message_id == "b"


# ---------------------------------------------------------------- 计划转换


def test_build_groups_root_and_keep():
    rows = [
        _m("a", T0),
        _m("b", T0 + 3 * HOUR, parent_message_id="a"),
        _m("c", T0 + 3 * HOUR + 1000, parent_message_id="b"),
    ]
    groups = build_split_groups("a", plan_pieces(rows))
    assert [g["root_message_id"] for g in groups] == ["a", "b"]
    assert [g["keep"] for g in groups] == [True, False]
    assert [len(g["message_ids"]) for g in groups] == [1, 2]
    assert all(g["topic"] == "" for g in groups), "切分后不继承旧标签"


def test_build_groups_rejects_missing_message_id():
    rows = [_m("a", T0), _m("", T0 + 3 * HOUR), _m("c", T0 + 4 * HOUR)]
    try:
        build_split_groups("a", plan_pieces(rows))
        assert False, "应抛 ValueError"
    except ValueError as e:
        assert "message_id" in str(e)


# ---------------------------------------------------------------- 集成:落库


def _setup():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    svc = MessageService(session, allowed_chat_ids=set())
    return svc, session, Session


def _msg(mid, sender, ts, text, parent="", message_type="text"):
    return MessageCreate(
        message_id=mid,
        chat_id=CHAT,
        sender_id=sender,
        sender_type="user",
        message_type=message_type,
        text=text,
        create_time=str(ts),
        parent_message_id=parent,
    )


def _two_problem_group(session):
    """一个对话里混两个问题,第二个问题隔 3 小时,且回复的是上一问题的消息。

    这是**存量畸形对话**:入库护栏(`case_split_gap_seconds`,默认 2h)上线后,
    新数据回复久无动静的旧组会自成一组,这种混合组再也长不出来。切分脚本的
    服务对象正是护栏之前已经写进库的历史数据,所以播种时把护栏关掉
    (`gap=0`)复现旧行为,播完立刻恢复,避免污染同进程内其他测试。
    """
    from app.core.config import get_settings

    settings = get_settings()
    old_gap = settings.case_split_gap_seconds
    settings.case_split_gap_seconds = 0
    try:
        svc = MessageService(session, allowed_chat_ids=set())
        svc.save_message(_msg("m1", "u1", T0, "这台电视画面模糊"))
        svc.save_message(_msg("m2", "u2", T0 + 60_000, "换个片源试试", parent="m1"))
        svc.save_message(_msg("m3", "u1", T0 + 120_000, "换了还是一样", parent="m2"))
        # 第二件不相关的事:回复的是上一问题的 m2(跨切点回复边)
        svc.save_message(_msg("m4", "u3", T0 + 3 * HOUR, "另一台开不了机", parent="m2"))
        svc.save_message(
            _msg("m5", "u2", T0 + 3 * HOUR + 60_000, "师傅已上门处理", parent="m4")
        )
    finally:
        settings.case_split_gap_seconds = old_gap
    conv = session.query(Conversation).filter_by(root_message_id="m1").one()
    # 打上「混合组」的旧标签,模拟归并造成的错标
    for m in session.query(Message).all():
        m.topic = "画面模糊"
    conv.topic = "画面模糊"
    session.commit()
    return conv.id


def test_dry_run_does_not_touch_db():
    _, session, _ = _setup()
    conv_id = _two_problem_group(session)
    report = split_conversation_by_case(session, CHAT, conv_id, apply=False)
    assert report["split"] is True
    assert report["pieces"] == 2
    assert report["applied"] is False
    assert report["cut_reasons"] == [CUT_START, CUT_GAP]
    assert len(session.query(Conversation).all()) == 1
    assert session.query(Message).filter_by(message_id="m4").one().root_message_id == "m1"
    session.close()


def test_apply_splits_and_resets_topic():
    _, session, _ = _setup()
    conv_id = _two_problem_group(session)
    report = split_conversation_by_case(session, CHAT, conv_id, apply=True)
    assert report["applied"] is True

    assert len(session.query(Conversation).all()) == 2
    conv_a = session.query(Conversation).filter_by(root_message_id="m1").one()
    conv_b = session.query(Conversation).filter_by(root_message_id="m4").one()
    assert conv_a.id == conv_id, "keep 组沿用原对话行"
    assert conv_b.id != conv_id

    # 消息重挂
    roots = {
        m.message_id: m.root_message_id for m in session.query(Message).all()
    }
    assert roots == {"m1": "m1", "m2": "m1", "m3": "m1", "m4": "m4", "m5": "m4"}

    # keep 组保留原标签;新单元清空等重新归类
    assert conv_a.topic == "画面模糊"
    assert conv_b.topic == ""
    for mid in ("m4", "m5"):
        m = session.query(Message).filter_by(message_id=mid).one()
        assert m.topic == "", mid
        assert m.topic_conf == 0.0
    # 阶段跟着组内文本重算:新单元有「师傅已上门处理」→ 已上门(不是继承旧的空值)
    assert conv_b.stage == "已上门"
    assert conv_a.stage == "待处理"

    # 统计
    assert (conv_a.message_count, conv_a.participant_count) == (3, 2)
    assert (conv_b.message_count, conv_b.participant_count) == (2, 2)

    # 两条树都不丢消息:新单元的 m4 回复的是上一单元的 m2,
    # 父不在本对话 → m4 按根渲染,不会消失
    repo = ConversationRepo(session)
    tree_b = repo.list_tree(conv_b.id)
    assert tree_b["message_count"] == 2
    assert [r["message"]["message_id"] for r in tree_b["roots"]] == ["m4"]
    assert [c["message"]["message_id"] for c in tree_b["roots"][0]["children"]] == ["m5"]

    tree_a = repo.list_tree(conv_a.id)
    assert tree_a["message_count"] == 3
    assert [r["message"]["message_id"] for r in tree_a["roots"]] == ["m1"]
    session.close()


def test_apply_is_idempotent():
    _, session, _ = _setup()
    conv_id = _two_problem_group(session)
    split_conversation_by_case(session, CHAT, conv_id, apply=True)
    conv_b = session.query(Conversation).filter_by(root_message_id="m4").one()

    # 再切一次:两块内部都无切点
    report = split_conversation_by_case(session, CHAT, conv_id, apply=True)
    assert report["split"] is False
    report_b = split_conversation_by_case(session, CHAT, conv_b.id, apply=True)
    assert report_b["split"] is False
    assert len(session.query(Conversation).all()) == 2
    session.close()


def test_no_cut_point_returns_early():
    _, session, _ = _setup()
    svc = MessageService(session, allowed_chat_ids=set())
    svc.save_message(_msg("s1", "u1", T0, "只有一个问题"))
    svc.save_message(_msg("s2", "u2", T0 + 60_000, "收到", parent="s1"))
    conv = session.query(Conversation).filter_by(root_message_id="s1").one()
    report = split_conversation_by_case(session, CHAT, conv.id, apply=True)
    assert report["split"] is False
    assert report["reason"] == "无可切点"
    assert len(session.query(Conversation).all()) == 1
    session.close()


def test_chat_mismatch_raises():
    _, session, _ = _setup()
    conv_id = _two_problem_group(session)
    try:
        split_conversation_by_case(session, "oc_other", conv_id)
        assert False, "应抛 ValueError"
    except ValueError as e:
        assert "chat" in str(e)
    session.close()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
