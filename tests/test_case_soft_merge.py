"""案例软合并测试(阶段三,内存 SQLite + 假 LLM)。

验证验收 V2–V7:
- V2 软合并不破坏数据:只写 case_id,root/conversation_id 不变,行数不变;
- V3 确定性通道免 LLM(同 case_key 缝合);
- V4 幂等(同组再次缝合无新关联);
- V5 解绑可回滚(case_id 归 0,数据零损失);
- V6 确认合并(物理收敛为一行,case_id 归 0);
- V7 主题冲突护栏仍拦截(即使 case_key 相同)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Conversation, Message
from app.schemas.message import MessageCreate
from app.services.case_merge import (
    merge_case,
    resolve_case_root,
    soft_link_cases,
    unlink_case,
)
from app.services.conv_stitch import plan_stitches, stitch_chat
from app.services.message_service import MessageService

T0 = 1700000000000
CHAT = "oc_case"


class FakeLLM:
    """按队列返回输出;只剩一个时固定复用。带调用计数。"""

    def __init__(self, *outputs: str):
        self.outputs = list(outputs)
        self.calls: list[str] = []

    def chat(self, system: str, user: str) -> str:
        self.calls.append(user)
        if len(self.outputs) > 1:
            return self.outputs.pop(0)
        return self.outputs[0]


def _setup():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    svc = MessageService(session, allowed_chat_ids=set())
    return svc, session, Session


def _msg(mid, sender, ts, text):
    return MessageCreate(
        message_id=mid,
        chat_id=CHAT,
        sender_id=sender,
        sender_type="user",
        message_type="text",
        text=text,
        create_time=str(ts),
    )


def _three_case(svc, session):
    """3 个独立会话(无回复链),同一问题。"""
    svc.save_message(_msg("m1", "u1", T0, text="住了很久的电视爆音"))
    svc.save_message(_msg("m2", "u2", T0 + 120_000, text="帮看看"))
    svc.save_message(_msg("m3", "u3", T0 + 300_000, text="后台发一下处理结果"))
    assert len(session.query(Conversation).all()) == 3


def test_soft_link_only_writes_case_id():
    """V2:软合并不破坏数据——只写 case_id,行数/root/conversation_id 全不变。"""
    svc, session, Session = _setup()
    _three_case(svc, session)
    llm = FakeLLM("1,2,3")

    done = stitch_chat(session, CHAT, llm)
    assert len(done) == 2

    convs = session.query(Conversation).order_by(Conversation.id).all()
    assert len(convs) == 3, "软合并不应删会话行"
    case_ids = {c.case_id for c in convs}
    assert len(case_ids) == 1, f"三会话应共享同一 case_id,实际 {case_ids}"
    rep = convs[0]
    assert rep.case_id == rep.id, "代表自指向"
    # 消息归属完全不变
    msgs = {m.message_id: m for m in session.query(Message).all()}
    for m in ("m1", "m2", "m3"):
        assert msgs[m].root_message_id == m, f"{m} root 不应被改写"
    print("V2 soft link only writes case_id OK")


def test_soft_link_deterministic_no_llm():
    """V3:case_key 相同的会话软合并,确定性通道不调 LLM。"""
    svc, session, Session = _setup()
    svc.save_message(_msg("m1", "u1", T0, text="序列号:ABC12345 爆音了"))
    svc.save_message(_msg("m2", "u2", T0 + 120_000, text="SN:ABC12345 还没好"))
    svc.save_message(_msg("m3", "u3", T0 + 300_000, text="SN:ABC12345 已解决"))
    llm = FakeLLM("1,2,3")  # 真实场景本不该被调用

    done = stitch_chat(session, CHAT, llm)
    assert len(done) == 2
    assert llm.calls == [], f"确定性锚点不应调用 LLM,实际 {len(llm.calls)} 次"
    convs = session.query(Conversation).all()
    assert len({c.case_id for c in convs}) == 1
    print("V3 deterministic stitch no llm OK")


def test_soft_link_idempotent():
    """V4:同组再次缝合幂等——无新关联、无异常。"""
    svc, session, Session = _setup()
    _three_case(svc, session)
    stitch_chat(session, CHAT, FakeLLM("1,2,3"))
    convs_before = {
        c.root_message_id: (c.id, c.case_id) for c in session.query(Conversation).all()
    }
    assert stitch_chat(session, CHAT, FakeLLM("1,2,3")) == []
    convs_after = {
        c.root_message_id: (c.id, c.case_id) for c in session.query(Conversation).all()
    }
    assert convs_before == convs_after, "幂等:再次缝合不应改变任何 case_id"
    print("V4 idempotent OK")


def test_unlink_rollback():
    """V5:解绑回滚——case_id 全归 0,行/消息归属与解绑前一致。"""
    svc, session, Session = _setup()
    _three_case(svc, session)
    stitch_chat(session, CHAT, FakeLLM("1,2,3"))
    rep = session.query(Conversation).order_by(Conversation.id).first()
    case_id = rep.case_id
    assert case_id != 0
    msgs_before = {
        m.message_id: (m.root_message_id, m.conversation_id)
        for m in session.query(Message).all()
    }

    n = unlink_case(session, CHAT, case_id, commit=True)
    assert n == 3, f"应解绑 3 个会话,实际 {n}"
    assert len(session.query(Conversation).all()) == 3, "解绑不删行"
    assert all(
        session.query(Conversation).filter_by(root_message_id=r).one().case_id == 0
        for r in ("m1", "m2", "m3")
    )
    msgs_after = {
        m.message_id: (m.root_message_id, m.conversation_id)
        for m in session.query(Message).all()
    }
    assert msgs_before == msgs_after, "解绑后消息归属必须完全不变"
    print("V5 unlink rollback OK")


def test_confirm_merge_physical():
    """V6:确认合并——组内会话物理收敛为最早根,case_id 归 0。"""
    svc, session, Session = _setup()
    _three_case(svc, session)
    stitch_chat(session, CHAT, FakeLLM("1,2,3"))
    rep = session.query(Conversation).order_by(Conversation.id).first()
    case_id = rep.case_id

    merged = merge_case(session, CHAT, case_id)
    assert merged == 2, f"应物理合并 2 个源会话,实际 {merged}"
    convs = session.query(Conversation).all()
    assert len(convs) == 1, f"确认合并后应只剩 1 行,实际 {len(convs)}"
    assert convs[0].root_message_id == "m1"
    assert convs[0].case_id == 0, "物理合并后 case_id 应归 0"
    msgs = {m.message_id: m for m in session.query(Message).all()}
    assert msgs["m2"].root_message_id == "m1"
    assert msgs["m3"].conversation_id == convs[0].id
    print("V6 confirm merge physical OK")


def test_topic_conflict_guard():
    """V7:主题冲突护栏——topic 不同时即使 case_key 相同也拒绝软合并。"""
    svc, session, Session = _setup()
    svc.save_message(_msg("m1", "u1", T0, text="设备SN:X11111 爆音"))
    svc.save_message(_msg("m2", "u2", T0 + 120_000, text="SN:X11111 画质模糊"))
    for root, topic in (("m1", "爆音"), ("m2", "画质")):
        session.query(Conversation).filter_by(root_message_id=root).one().topic = topic
    session.commit()

    llm = FakeLLM("1,2")
    assert plan_stitches(session, CHAT, llm) == [], "主题冲突应拦截"
    assert stitch_chat(session, CHAT, llm) == []
    convs = session.query(Conversation).all()
    assert all(c.case_id == 0 for c in convs), "不应产生任何软关联"
    print("V7 topic conflict guard OK")


def test_root_resolution_flattens_chain():
    """链式软合并 A→B→C 后仍对齐到最早代表,无漂移。"""
    svc, session, Session = _setup()
    for i in range(1, 4):
        svc.save_message(_msg(f"m{i}", f"u{i}", T0 + i * 120_000, text=f"消息{i}"))
    convs = {c.root_message_id: c for c in session.query(Conversation).all()}
    c1, c2, c3 = convs["m1"].id, convs["m2"].id, convs["m3"].id

    assert soft_link_cases(session, CHAT, c2, c1) is True   # m2 -> m1 组
    assert soft_link_cases(session, CHAT, c3, c2) is True   # m3 -> (m2所在组=m1)
    rep = resolve_case_root(session, c3)
    assert rep.root_message_id == "m1", "链式后代表应为 m1,而非中间节点"
    case_ids = {c.case_id for c in session.query(Conversation).all()}
    assert len(case_ids) == 1 and case_ids == {rep.id}, f"整组应拍平,实际 {case_ids}"
    print("root resolution flattens chain OK")


def test_same_group_link_idempotent_false():
    svc, session, Session = _setup()
    svc.save_message(_msg("m1", "u1", T0, text="问题一"))
    svc.save_message(_msg("m2", "u2", T0 + 120_000, text="问题一处理"))
    convs = {c.root_message_id: c for c in session.query(Conversation).all()}
    assert soft_link_cases(session, CHAT, convs["m2"].id, convs["m1"].id) is True
    assert soft_link_cases(session, CHAT, convs["m2"].id, convs["m1"].id) is False, "同组再并应返回 False"
    print("same-group idempotent False OK")


def test_migration_adds_case_id_column():
    """V1:存量 conversations 表缺 case_id 列时,轻量迁移能补齐,且二次调用幂等。"""
    import app.core.db as db_mod
    from sqlalchemy import inspect, text

    engine2 = create_engine("sqlite://")
    with engine2.begin() as conn:
        # 模拟存量库:conversations 表没有 case_id 列
        conn.execute(text(
            "CREATE TABLE conversations ("
            "id INTEGER PRIMARY KEY, chat_id VARCHAR(128), "
            "root_message_id VARCHAR(128), topic VARCHAR(128) DEFAULT '')"
        ))
    old_engine = db_mod.engine
    db_mod.engine = engine2
    try:
        db_mod._ensure_conversation_case_id()
        cols = {c["name"] for c in inspect(engine2).get_columns("conversations")}
        assert "case_id" in cols, f"迁移后应含 case_id,实际 {cols}"
        # 幂等:列已存在,再调不报错也不重复加
        db_mod._ensure_conversation_case_id()
        cols = {c["name"] for c in inspect(engine2).get_columns("conversations")}
        assert "case_id" in cols
    finally:
        db_mod.engine = old_engine
    print("V1 migration adds case_id OK")


if __name__ == "__main__":
    test_soft_link_only_writes_case_id()
    test_soft_link_deterministic_no_llm()
    test_soft_link_idempotent()
    test_unlink_rollback()
    test_confirm_merge_physical()
    test_topic_conflict_guard()
    test_root_resolution_flattens_chain()
    test_same_group_link_idempotent_false()
    print("ALL CASE SOFT MERGE TESTS PASSED")
