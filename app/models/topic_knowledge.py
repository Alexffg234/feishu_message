"""主题参考知识库模型。

topic_knowledge:按 topic 名挂标准排查/解决手册(如《投屏常见市场问题自查手册》
拆分到各小类)。生成主题 AI 分析时作为参考知识拼进 LLM 上下文,
让「如何解决」贴合标准自查流程。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.message import BigIntPK, Base, _utcnow


class TopicKnowledge(Base):
    __tablename__ = "topic_knowledge"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    topic: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<TopicKnowledge {self.topic!r} len={len(self.content or '')}>"
