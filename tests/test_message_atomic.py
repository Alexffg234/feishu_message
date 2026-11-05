"""H1 消息入库原子化回归测试(内存/临时文件 SQLite)。

验证:
1. 「消息 + 对话挂接」收敛为单事务后,attach 阶段抛异常时消息不落孤儿,
   会话整体回滚干净且可复用。
2. 并发建对话「存在即复用」:两事务同 (chat, root),后者唯一键冲突后
   复用既有对话行而不是向上抛错(或误判为重复入库)。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Conversation, Message, MessageLink
from app.services.message_service import MessageService
from app.dao.message_repo import MessageRepo
from app.dao.conversation_repo import ConversationRepo
from app.schemas.message import MessageCreate


def _make_service():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    svc = MessageService(session, allowed_chat_ids=set())
    return svc, session


def _msg(mid, sender, chat="oc_atomic", parent="", root=""):
    return MessageCreate(
        message_id=mid,
        chat_id=chat,
        sender_id=sender,
        sender_type="user",
        message_type="text",
        text=f"msg-{mid}",
        create_time="1700000000000",
        parent_message_id=parent,
        root_message_id=root,
    )


def test_attach_failure_no_orphan(monkeypatch):
    """attach 阶段抛异常 → 消息与对话整体回滚,不落孤儿,会话干净可复用。"""
    svc, session = _make_service()
    orig_attach = svc._attach_conversation  # 绑定方法,供后续恢复

    def boom(msg):
        raise RuntimeError("attach fail")

    monkeypatch.setattr(svc, "_attach_conversation", boom)

    # 单事务下 attach 失败 → 整笔回滚并向上抛出,不被误判成「重复入库」
    with pytest.raises(RuntimeError):
        svc.save_message(_msg("m1", "u1"))

    # 无消息、无对话、无回复边残留(半提交窗口已被消除)
    assert session.query(Message).count() == 0
    assert session.query(Conversation).count() == 0
    assert session.query(MessageLink).count() == 0
    # 回滚后 exists 应为 False(不再把自身半提交误当重复)
    assert MessageRepo(session).exists("oc_atomic", "m1") is False

    # 恢复原 attach 后验证回滚后的会话干净、可复用
    monkeypatch.setattr(svc, "_attach_conversation", orig_attach)
    assert svc.save_message(_msg("m2", "u1")) is True
    assert session.query(Message).count() == 1
    assert session.query(Conversation).count() == 1


def test_concurrent_conversation_create_reuse(monkeypatch, tmp_path):
    """并发建对话「存在即复用」:两事务同 (chat, root),
    后到者在唯一键上冲突,回退复用既有对话行而非抛错/重复建对话。"""
    db = tmp_path / "atomic.db"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    # 「先到者」事务:建好对话并提交
    s1 = Session()
    conv_a = ConversationRepo(s1).create("oc_tx", "r1", "u1")
    s1.commit()
    conv_a_id = conv_a.id
    s1.close()

    # 「后到者」事务:get 第一次返回 None(模拟两 worker 都 miss 的竞态窗口),
    # 随后 create 真实命中唯一键 → 逻辑应回退复用而不抛错
    s2 = Session()
    svc2 = MessageService(s2, allowed_chat_ids=set())
    orig_get = ConversationRepo.get
    n = {"calls": 0}

    def fake_get(self, chat_id, root_message_id):
        n["calls"] += 1
        if n["calls"] == 1:
            return None  # 竞态窗口:没看到已提交的对话
        return orig_get(self, chat_id, root_message_id)

    monkeypatch.setattr(ConversationRepo, "get", fake_get)

    # 同 root 的回复消息走完整入库管道(内部 commit)
    assert svc2.save_message(_msg("b1", "u2", chat="oc_tx", parent="r1", root="r1")) is True

    # 未新增对话(复用既有),消息挂到既有对话
    check = Session()
    try:
        assert check.query(Conversation).filter_by(
            chat_id="oc_tx", root_message_id="r1"
        ).count() == 1
        m = check.query(Message).filter(Message.message_id == "b1").one()
        assert m.conversation_id == conv_a_id
    finally:
        check.close()
    s2.close()


def test_savepoint_rollback_reuse_commits_full_tx(monkeypatch, tmp_path):
    """SAVEPOINT 冲突回退复用后,外层「消息事务」仍完整提交。

    补盲:相比 test_concurrent_conversation_create_reuse(只断言对话复用 +
    消息 conversation_id),本用例额外验证冲突回退后同一事务内的回复边、
    对话聚合统计也一并落库——即外层事务在 SAVEPOINT 回滚后依旧可用并整体提交,
    不会残留未 flush 的重复 Conversation。
    """
    db = tmp_path / "atomic_full.db"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    # 「先到者」已经提交了 (oc_full, r1) 对话
    s1 = Session()
    conv_a = ConversationRepo(s1).create("oc_full", "r1", "u1")
    s1.commit()
    conv_a_id = conv_a.id
    s1.close()

    # 「后到者」:首次 get miss(竞态窗口)→ create 真实命中唯一键 → 回退复用
    s2 = Session()
    svc2 = MessageService(s2, allowed_chat_ids=set())
    orig_get = ConversationRepo.get
    n = {"calls": 0}

    def fake_get(self, chat_id, root_message_id):
        n["calls"] += 1
        if n["calls"] == 1:
            return None  # 竞态:没看到已提交的对话
        return orig_get(self, chat_id, root_message_id)

    monkeypatch.setattr(ConversationRepo, "get", fake_get)

    # 回复消息:parent=r1 未作为消息落库(悬空父)→ 深度回退 depth=1
    assert svc2.save_message(_msg("b1", "u2", chat="oc_full", parent="r1", root="r1")) is True

    check = Session()
    try:
        # 复用既有对话,不新增、不残留 pending 重复插入
        assert n["calls"] >= 2  # 确认确实走了「get→create失败→再 get」的复用路径
        assert check.query(Conversation).filter_by(
            chat_id="oc_full", root_message_id="r1"
        ).count() == 1
        m = check.query(Message).filter(Message.message_id == "b1").one()
        assert m.conversation_id == conv_a_id
        # 外层事务完整提交:回复边 + 聚合统计一并落库
        link = check.query(MessageLink).filter_by(child_message_id="b1").one()
        assert link.parent_message_id == "r1"
        assert link.depth == 1
        conv = check.query(Conversation).filter_by(id=conv_a_id).one()
        assert conv.message_count == 1
    finally:
        check.close()
    s2.close()

