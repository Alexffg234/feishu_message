"""入库侧「问题单元」护栏:回复久无动静的旧组时新起一组。

背景:群里常见「点着一条三天前的消息回复,问的却是全新问题」。旧行为是
`_resolve_root` 无条件 `return parent.root_message_id`,新问题直接被挂进旧
对话——线上那些横跨 4 天、混了 4 件不相关事情的巨型对话就是这么长出来的。

护栏规则:整组最后一条消息距当前消息超过 `case_split_gap_seconds`(默认 2h)
→ 当前消息自成新组;是否仍属同一案例交给会话缝合(conv_stitch)软判定并打
case_id,可解绑,不做物理合并。

这里锁住四件事:
1. 隔天回复 → 新组,旧组消息数不变;
2. 组内刚有人说话时的回复 → 仍并入旧组(不误切);
3. gap=0 → 关闭护栏,恢复旧行为;
4. 两边树都不丢、不重复,跨组边的 depth 按 1 记。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.dao.conversation_repo import ConversationRepo
from app.models import Base, Conversation, Message, MessageLink
from app.schemas.message import MessageCreate
from app.services.message_service import MessageService

T0 = 1700000000000
SEC = 1000
MIN = 60 * SEC
HOUR = 60 * MIN
DAY = 24 * HOUR
CHAT = "oc_gap_guard"


def _setup():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    svc = MessageService(session, allowed_chat_ids=set())
    return svc, session


def _msg(mid, ts, text, parent="", sender="u1"):
    return MessageCreate(
        message_id=mid,
        chat_id=CHAT,
        sender_id=sender,
        sender_type="user",
        message_type="text",
        text=text,
        create_time=str(ts),
        parent_message_id=parent,
    )


def _with_gap(seconds: int):
    """临时改护栏阈值(测试后还原),返回 (旧值, setter)。"""
    st = get_settings()
    old = st.case_split_gap_seconds
    st.case_split_gap_seconds = seconds
    return old


def _restore(seconds: int) -> None:
    get_settings().case_split_gap_seconds = seconds


def _seed_old_group(svc):
    """一个 3 天前的问题组:m1 ← m2。"""
    svc.save_message(_msg("m1", T0, "电视画面模糊"))
    svc.save_message(_msg("m2", T0 + MIN, "换个片源试试", parent="m1"))


def test_reply_to_old_group_starts_new_unit():
    old = _with_gap(7200)
    try:
        svc, session = _setup()
        _seed_old_group(svc)
        # 3 天后,有人点着 m2 回复,问的是另一台机器
        svc.save_message(_msg("m3", T0 + 3 * DAY, "另一台开不了机", parent="m2", sender="u3"))

        convs = session.query(Conversation).order_by(Conversation.id).all()
        assert len(convs) == 2, "隔天回复应新起一组"
        assert convs[0].root_message_id == "m1"
        assert convs[0].message_count == 2, "旧组不该被新问题污染"
        assert convs[1].root_message_id == "m3"
        assert convs[1].message_count == 1

        m3 = session.query(Message).filter_by(message_id="m3").one()
        assert m3.root_message_id == "m3"
        assert m3.parent_message_id == "m2", "仍保留「回复了哪条」的信息,只是不再并入该组"
    finally:
        _restore(old)


def test_reply_while_group_active_stays_together():
    """组内刚有人说过话(gap 内),回复旧消息属延续,不能切。"""
    old = _with_gap(7200)
    try:
        svc, session = _setup()
        svc.save_message(_msg("a1", T0, "投屏搜不到设备"))
        # 组内 2 小时后还有人说话(正好踩 gap 边界,严格大于才切)
        svc.save_message(_msg("a2", T0 + 2 * HOUR, "我在看日志", sender="u2", parent="a1"))
        # 现在有人回复最早那条 a1(距组最后一条仅 5 分钟)
        svc.save_message(
            _msg("a3", T0 + 2 * HOUR + 5 * MIN, "我也遇到了", parent="a1", sender="u3")
        )
        assert session.query(Conversation).count() == 1, "组还活跃,不该新起一组"
        assert session.query(Message).filter_by(message_id="a3").one().root_message_id == "a1"
    finally:
        _restore(old)


def test_gap_zero_disables_guard():
    old = _with_gap(0)
    try:
        svc, session = _setup()
        _seed_old_group(svc)
        svc.save_message(_msg("m3", T0 + 3 * DAY, "另一台开不了机", parent="m2"))
        assert session.query(Conversation).count() == 1, "gap=0 应恢复旧行为"
        assert session.query(Message).filter_by(message_id="m3").one().root_message_id == "m1"
    finally:
        _restore(old)


def test_parent_missing_from_db_unaffected():
    """父消息未采集到(库外)时仍按事件 anchor 分组,不受护栏影响。"""
    old = _with_gap(7200)
    try:
        svc, session = _setup()
        svc.save_message(_msg("x1", T0, "回复一条没采集到的旧消息", parent="ghost"))
        m = session.query(Message).filter_by(message_id="x1").one()
        assert m.root_message_id == "ghost"
        assert m.conversation_id != 0
    finally:
        _restore(old)


def test_trees_keep_every_message():
    """跨组边的两组树:不丢消息、不重复,新组按根渲染。"""
    old = _with_gap(7200)
    try:
        svc, session = _setup()
        _seed_old_group(svc)
        svc.save_message(_msg("m3", T0 + 3 * DAY, "另一台开不了机", parent="m2", sender="u3"))
        svc.save_message(
            _msg("m4", T0 + 3 * DAY + MIN, "师傅已上门处理", parent="m3", sender="u2")
        )
        repo = ConversationRepo(session)
        convs = session.query(Conversation).order_by(Conversation.id).all()
        tree_a = repo.list_tree(convs[0].id)
        tree_b = repo.list_tree(convs[1].id)
        assert tree_a["message_count"] == 2
        assert tree_b["message_count"] == 2
        assert [r["message"]["message_id"] for r in tree_a["roots"]] == ["m1"]
        assert [r["message"]["message_id"] for r in tree_b["roots"]] == ["m3"]
        assert [c["message"]["message_id"] for c in tree_b["roots"][0]["children"]] == ["m4"]

        # 跨组边留在新组、depth 按 1 记(不抄父树深度)
        link = session.query(MessageLink).filter_by(child_message_id="m3").one()
        assert link.conversation_id == convs[1].id
        assert link.depth == 1
    finally:
        _restore(old)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
