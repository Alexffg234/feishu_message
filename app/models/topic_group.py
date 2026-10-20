"""主题分组模型(问题大类)。

topic_groups:把若干具体 topic 归到一个问题大类下(如「投屏问题」
= 投屏找不到设备 + 镜像投屏卡顿 + 投屏卡顿)。前端按大类折叠展示,
大类下各小类保留各自的现象/排查/解决方案。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.message import BigIntPK, Base, JSONText, _utcnow


class TopicGroup(Base):
    __tablename__ = "topic_groups"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    # 组内 topic 名列表(按展示顺序): ["投屏找不到设备", "镜像投屏卡顿", ...]
    topics: Mapped[list] = mapped_column(JSONText, nullable=False, default=list)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<TopicGroup {self.name} topics={self.topics!r}>"
