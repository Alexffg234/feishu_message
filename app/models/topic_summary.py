"""主题 AI 总结存储模型。

topic_summaries:每个 (chat_id, topic) 一条 AI 分析,由后台定时任务全量预生成:
- 4 块文案:problem(问题是什么)/ investigation(如何排查)/ resolution(如何解决)/ status(当前状态)
- 指纹:message_count + max_message_id。topic 下出现新消息时指纹变化,
  下一轮定时任务自动重新生成(页面读缓存永远秒回)
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.message import BigIntPK, Base, JSONText, _utcnow


class TopicSummary(Base):
    __tablename__ = "topic_summaries"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    topic: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    problem: Mapped[str] = mapped_column(Text, nullable=False, default="")
    investigation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    resolution: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    # 生成时的指纹:该 topic 的消息数与最大消息 id
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    # 该 topic 对话中带可展示 URL 的媒体(图片/视频/文件),按出现顺序编号:
    # [{idx, type: image/media/file, url, name, at(毫秒时间戳字符串)}]
    # LLM 文案里用「图#N / 视频#N」引用,前端据此渲染缩略图
    media_refs: Mapped[Optional[list]] = mapped_column(JSONText, nullable=True, default=None)

    __table_args__ = (
        UniqueConstraint("chat_id", "topic", name="uq_topic_summary_chat_topic"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<TopicSummary {self.chat_id}/{self.topic} status={self.status!r}>"
