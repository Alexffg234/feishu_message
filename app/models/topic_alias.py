"""标签别名模型(话题归纳阶段一)。

topic_aliases:近似于「同义词词典」,把多个等价写法收敛到唯一标准标签。
- alias    -> canonical(收敛目标)
- canonical 可以是标准标签(问题大类名)或普通收敛标签
- auto     =1 表示由系统自动建议、待人工确认;=0 表示人工维护
LLM 打标输出的标签先过 label_norm.normalize_label 收敛,归组才按收敛后的标签相等。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.message import BigIntPK, Base, _utcnow


class TopicAlias(Base):
    __tablename__ = "topic_aliases"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    # 别名(等价写法),唯一
    alias: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    # 收敛到的标准标签
    canonical: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # 1=自动建议(待确认),0=人工维护
    auto: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<TopicAlias {self.alias!r} -> {self.canonical!r} auto={self.auto}>"
