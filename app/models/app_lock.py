"""调度器租约锁(轻量分布式互斥)。

scheduler_locks:保证同一时刻只有一个 gunicorn worker 执行周期性后台任务
(如主题总结全量预生成),避免多 worker 并发重复调用 LLM。

- 获取:行不存在则 INSERT;已存在但租约过期(或仍是自己持有)则 UPDATE 接管
- 释放:仅持有者(token 匹配)可删除
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.message import Base, _utcnow


class SchedulerLock(Base):
    __tablename__ = "scheduler_locks"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 持有者标识(uuid);释放时按 token 匹配,防误删他人租约
    token: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # 租约到期时间;到期后其他 worker 可接管
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SchedulerLock {self.name} token={self.token[:8]} expire={self.expires_at}>"
