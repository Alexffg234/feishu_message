"""话题段落模型。

表 message_segments 记录「整群话题段」——把群消息按时间流切成一段段
「在聊同一件事」的片段(方案Y:整群话题段)。

与消息/对话的关系:
- 一个段 = 一个话题的完整讨论,段内消息时间连续、内容相关
- messages 表新增 segment_id 外键指向本表;不删除原有 root_message_id / conversation_id,
  新旧归类并行,便于对比与回滚
- 段内的聊天不一定依赖飞书回复链(anchor 是时间 + 话题内容,而非回复元数据)
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.message import Base, BigIntPK, _utcnow


class MessageSegment(Base):
    __tablename__ = "message_segments"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # LLM 打的段主题(如"投屏问题");空=尚未分类(纯规则段)
    topic: Mapped[str] = mapped_column(String(128), nullable=False, default="", index=True)
    # 段时间范围(毫秒时间戳,与 messages.create_time 同源)
    start_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, index=True)
    end_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, index=True)
    # 段内第一条消息 message_id(段锚点)
    root_message_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    participant_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 是否被其他话题穿插(同一话题被打断又回来的交叉场景,仅标记不强行拆)
    interleaved: Mapped[bool] = mapped_column(Integer, nullable=False, default=0)

    # 时间范围唯一性约束:同一群内,(start_ms, end_ms) 应唯一,便于幂等重算时定位
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index("ix_segments_chat_start", "chat_id", "start_ms"),
        Index("ix_segments_chat_end", "chat_id", "end_ms"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<MessageSegment {self.id} {self.chat_id} "
            f"{self.start_ms}-{self.end_ms} topic={self.topic!r} n={self.message_count}>"
        )
