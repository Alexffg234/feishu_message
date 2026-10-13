"""对话关系数据访问层。

围绕 conversations(对话/回复链)与 message_links(回复边)两张表,
封装对话的建立、更新、查询,以及消息→对话、回复边的读写。

业务约定与 message_service 保持一致:
- 一条回复链 = 一个对话(conversation),唯一键 (chat_id, root_message_id)
- 独立消息自成一个对话(root_message_id = 自身 message_id)
- message_links 存每条 child→parent 回复边;depth 表示该子消息在树中的深度
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.conversation import Conversation, MessageLink
from app.models.message import Message

logger = logging.getLogger(__name__)


class ConversationRepo:
    def __init__(self, session: Session):
        self._session = session

    # ------------------------------------------------------------ conversations
    def get(self, chat_id: str, root_message_id: str) -> Conversation | None:
        stmt = select(Conversation).where(
            Conversation.chat_id == chat_id,
            Conversation.root_message_id == root_message_id,
        )
        return self._session.scalars(stmt).first()

    def create(self, chat_id: str, root_message_id: str, root_sender_id: str) -> Conversation:
        conv = Conversation(
            chat_id=chat_id,
            root_message_id=root_message_id,
            root_sender_id=root_sender_id,
            status="open",
            started_at_ms=0,
            last_message_at_ms=0,
        )
        self._session.add(conv)
        self._session.flush()  # 拿到 id 而不 commit,交给调用方统一提交
        return conv

    def update_group_stats(self, conversation_id: int) -> None:
        """重算对话的 message_count / participant_count / 时间范围。"""
        # 先 flush 挂起的 conversation_id 修改,确保统计能读到最新值
        self._session.flush()
        rows = self._session.execute(
            select(
                func.count(Message.id),
                func.count(func.distinct(Message.sender_id)),
                func.min(Message.create_time),
                func.max(Message.create_time),
            ).where(Message.conversation_id == conversation_id)
        ).one()
        count, participants, min_t, max_t = rows
        participants = int(participants or 0)
        try:
            min_ms = int(min_t) if min_t else 0
            max_ms = int(max_t) if max_t else 0
        except (TypeError, ValueError):
            min_ms = max_ms = 0
        self._session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(
                message_count=int(count or 0),
                participant_count=participants,
                started_at_ms=min_ms,
                last_message_at_ms=max_ms,
            )
        )

    # ------------------------------------------------------------ message_links
    def link_exists(self, parent_message_id: str, child_message_id: str) -> bool:
        stmt = select(MessageLink.id).where(
            MessageLink.parent_message_id == parent_message_id,
            MessageLink.child_message_id == child_message_id,
        )
        return self._session.execute(stmt).first() is not None

    def get_depth(self, message_id: str) -> int:
        """返回某消息当前在树中的深度;无则 -1。"""
        stmt = select(MessageLink.depth).where(MessageLink.child_message_id == message_id)
        row = self._session.execute(stmt).first()
        return int(row[0]) if row else -1

    def add_link(
        self,
        conversation_id: int,
        parent_message_id: str,
        child_message_id: str,
        parent_sender_id: str = "",
        child_sender_id: str = "",
        depth: int = 1,
    ) -> MessageLink | None:
        """新增一条回复边。已存在则跳过。"""
        if self.link_exists(parent_message_id, child_message_id):
            return None
        link = MessageLink(
            conversation_id=conversation_id,
            parent_message_id=parent_message_id,
            child_message_id=child_message_id,
            parent_sender_id=parent_sender_id,
            child_sender_id=child_sender_id,
            depth=depth,
        )
        self._session.add(link)
        self._session.flush()
        return link

    # ------------------------------------------------------------ 查询(树形)
    def list_recent(self, chat_id: str | None = None, limit: int = 50) -> list[Conversation]:
        """最近活跃的对话(按最后消息时间倒序)。"""
        stmt = (
            select(Conversation)
            .order_by(Conversation.last_message_at_ms.desc(), Conversation.id.desc())
            .limit(limit)
        )
        if chat_id:
            stmt = stmt.where(Conversation.chat_id == chat_id)
        return list(self._session.scalars(stmt).all())

    def list_by_conversation(self, conversation_id: int) -> list[Message]:
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.received_at.asc(), Message.id.asc())
        )
        return list(self._session.scalars(stmt).all())

    @staticmethod
    def _tree_dict(m: Message) -> dict:
        """把 Message ORM 对象转成可 JSON 序列化的字典。"""
        return {
            "message_id": m.message_id,
            "sender_id": m.sender_id,
            "sender_type": m.sender_type,
            "message_type": m.message_type,
            "text": m.text,
            "create_time": m.create_time,
            "parent_message_id": m.parent_message_id,
            "topic": m.topic,
        }

    def list_tree(self, conversation_id: int) -> dict:
        """返回对话的树形结构:{root_message, children:[{message, children:[...]}]}。

        children 按回复时间升序。
        """
        messages = self.list_by_conversation(conversation_id)
        by_id = {m.message_id: m for m in messages}
        links = self._session.execute(
            select(MessageLink).where(MessageLink.conversation_id == conversation_id)
        ).scalars().all()

        # 建立 parent -> [child_message_id] 映射(保持插入顺序)
        children_of: dict[str, list[MessageLink]] = {}
        for link in links:
            children_of.setdefault(link.parent_message_id, []).append(link)
        for arr in children_of.values():
            arr.sort(key=lambda l: l.created_at)

        def build(node_id: str):
            node = by_id.get(node_id)
            children = [
                build(link.child_message_id)
                for link in children_of.get(node_id, [])
                if link.child_message_id in by_id
            ]
            return {"message": self._tree_dict(node), "children": children}

        roots = []
        child_ids = {link.child_message_id for link in links}
        for m in messages:
            if m.message_id not in child_ids:
                roots.append(build(m.message_id))
        return {"roots": roots, "message_count": len(messages)}
