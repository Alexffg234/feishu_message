"""话题段落数据访问层 (DAO)。

封装 message_segments 表与 messages.segment_id 的读写,
供离线重算与增量归段使用。
"""
from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.message import Message
from app.models.segment import MessageSegment


class SegmentRepo:
    """MessageSegment 表的存取操作。"""

    def __init__(self, session: Session):
        self._session = session

    # ------------------------------------------------------------ 读取
    def list_chat_ids(self) -> list[str]:
        """返回存在消息的全部群 chat_id。"""
        stmt = select(Message.chat_id).distinct().order_by(Message.chat_id)
        return [r[0] for r in self._session.execute(stmt).all()]

    def list_messages_by_chat(self, chat_id: str) -> list[Message]:
        """取某群全部消息,按 received_at 正序(辅助用 id 稳定排序)。"""
        stmt = (
            select(Message)
            .where(Message.chat_id == chat_id)
            .order_by(Message.received_at.asc(), Message.id.asc())
        )
        return list(self._session.scalars(stmt).all())

    def list_segments(self, chat_id: str) -> list[MessageSegment]:
        stmt = (
            select(MessageSegment)
            .where(MessageSegment.chat_id == chat_id)
            .order_by(MessageSegment.start_ms.asc())
        )
        return list(self._session.scalars(stmt).all())

    # ------------------------------------------------------------ 写入
    def insert_segment(self, seg: MessageSegment) -> MessageSegment:
        """新增一段。"""
        self._session.add(seg)
        self._session.flush()  # 取到 seg.id,由调用方负责 commit
        return seg

    def set_segment_for_messages(self, segment_id: int, message_ids: list[int]) -> int:
        """批量把一段内的消息回写 segment_id,返回影响行数。"""
        if not message_ids:
            return 0
        stmt = (
            update(Message)
            .where(Message.id.in_(message_ids))
            .values(segment_id=segment_id)
        )
        return self._session.execute(stmt).rowcount or 0

    def clear_chat_segment(self, chat_id: str) -> int:
        """幂等清理:删除某群所有旧段,并把消息 segment_id 归零。

        返回被删除的段数。重算某群前调用,保证可反复跑。
        """
        segs = self.list_segments(chat_id)
        if segs:
            for s in segs:
                self._session.delete(s)
            self._session.flush()
        # 归零该群所有消息的 segment_id
        stmt = update(Message).where(Message.chat_id == chat_id).values(segment_id=0)
        self._session.execute(stmt)
        return len(segs)
