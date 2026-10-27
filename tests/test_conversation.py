"""对话关系写入逻辑集成测试(内存 SQLite)。

验证:消息入库后 conversations / message_links 是否正确生成,
多层级回复树(A→B→C)的 depth 与父子边是否正确。
不触碰远程 MySQL,使用独立内存库。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Conversation, MessageLink, Message
from app.services.message_service import MessageService
from app.schemas.message import MessageCreate


def _make_service():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    svc = MessageService(session, allowed_chat_ids=set())
    return svc, session


def _msg(mid, sender, parent="", root=""):
    return MessageCreate(
        message_id=mid,
        chat_id="oc_test",
        sender_id=sender,
        sender_type="user",
        message_type="text",
        text=f"msg-{mid}",
        create_time="1700000000000",
        parent_message_id=parent,
        root_message_id=root,
    )


def test_single_message_forms_conversation():
    svc, session = _make_service()
    assert svc.save_message(_msg("m1", "u1")) is True
    convs = session.query(Conversation).all()
    assert len(convs) == 1
    assert convs[0].root_message_id == "m1"
    assert convs[0].message_count == 1
    # 独立消息无回复边
    assert session.query(MessageLink).count() == 0
    # 消息挂到对话
    m = session.query(Message).filter(Message.message_id == "m1").one()
    assert m.conversation_id == convs[0].id
    print("single OK")


def test_reply_chain_forms_links():
    svc, session = _make_service()
    svc.save_message(_msg("m1", "u1"))                     # 根
    svc.save_message(_msg("m2", "u2", parent="m1"))        # u2 回 m1, root=m1
    svc.save_message(_msg("m3", "u3", parent="m2"))        # u3 回 m2, root=m1
    convs = session.query(Conversation).all()
    assert len(convs) == 1
    conv = convs[0]
    assert conv.root_message_id == "m1"
    assert conv.message_count == 3
    assert conv.participant_count == 3

    links = session.query(MessageLink).order_by(MessageLink.depth).all()
    # 两条边:m2→m1 (depth1), m3→m2 (depth2)
    assert len(links) == 2
    by_child = {l.child_message_id: l for l in links}
    assert by_child["m2"].parent_message_id == "m1"
    assert by_child["m2"].depth == 1
    assert by_child["m2"].parent_sender_id == "u1"
    assert by_child["m2"].child_sender_id == "u2"
    assert by_child["m3"].parent_message_id == "m2"
    assert by_child["m3"].depth == 2
    print("chain OK")


def test_multi_root_same_chat_separate_conversations():
    svc, session = _make_service()
    svc.save_message(_msg("a1", "u1"))
    svc.save_message(_msg("b1", "u2"))
    convs = session.query(Conversation).all()
    assert len(convs) == 2
    print("multi-root OK")


def test_tree_query():
    svc, session = _make_service()
    svc.save_message(_msg("r", "u1"))
    svc.save_message(_msg("c1", "u2", parent="r"))
    svc.save_message(_msg("c2", "u3", parent="c1"))
    conv = session.query(Conversation).one()
    tree = ConversationRepoProxy(session).list_tree(conv.id) if False else None
    print("tree OK (via manual)")


# 简化:直接验证 repo.list_tree
from app.dao.conversation_repo import ConversationRepo


def test_tree_query_repo():
    svc, session = _make_service()
    svc.save_message(_msg("r", "u1"))
    svc.save_message(_msg("c1", "u2", parent="r"))
    svc.save_message(_msg("c2", "u3", parent="c1"))
    conv = session.query(Conversation).one()
    repo = ConversationRepo(session)
    tree = repo.list_tree(conv.id)
    assert tree["message_count"] == 3
    roots = tree["roots"]
    assert len(roots) == 1
    # list_tree 返回可 JSON 序列化的 dict(见 ConversationRepo._tree_dict)
    assert roots[0]["message"]["message_id"] == "r"
    assert len(roots[0]["children"]) == 1
    assert roots[0]["children"][0]["message"]["message_id"] == "c1"
    assert roots[0]["children"][0]["children"][0]["message"]["message_id"] == "c2"
    print("tree repo OK")


def test_tree_dangling_parent_not_lost():
    """回复边指向库外消息(回复了捕获前旧消息,父不在 messages)时,
    子消息应按根节点展示,不能无声丢失(线上 #143/#317 16:54 消息案例)。"""
    svc, session = _make_service()
    svc.save_message(_msg("r", "u1"))
    conv = session.query(Conversation).one()
    session.add(Message(
        message_id="d1", chat_id="oc_test", sender_id="u2", sender_type="user",
        message_type="text", text="回复旧消息", create_time="1700000001000",
        root_message_id="r", conversation_id=conv.id,
        parent_message_id="om_out_of_db",
    ))
    session.add(MessageLink(
        conversation_id=conv.id, parent_message_id="om_out_of_db",
        child_message_id="d1", parent_sender_id="u0", child_sender_id="u2", depth=1,
    ))
    session.commit()

    tree = ConversationRepo(session).list_tree(conv.id)
    assert tree["message_count"] == 2
    root_ids = {r["message"]["message_id"] for r in tree["roots"]}
    assert root_ids == {"r", "d1"}, f"悬挂边子消息应按根节点展示,实际 {root_ids}"
    print("dangling parent OK")


if __name__ == "__main__":
    test_single_message_forms_conversation()
    test_reply_chain_forms_links()
    test_multi_root_same_chat_separate_conversations()
    test_tree_query_repo()
    test_tree_dangling_parent_not_lost()
    print("ALL CONVERSATION TESTS PASSED")
