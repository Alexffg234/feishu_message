"""主题分类租约锁(多 worker 互斥)。

topic_classify_locks:按 (chat_id, root_message_id) 唯一键,保证同一回复链组
同时只有一个 gunicorn worker 对其调 LLM 分类,避免跨进程重复调用 LLM。

- INSERT 抢占:并发下唯一键冲突即说明别人已抢到,直接跳过
- status='done' 表示该组已分类完成,后续调度幂等跳过
- status='pending' 表示正在分类/排队:
  - 无 retry_at = 在途任务;claimed_at 租约未超时前其他 worker 不得接管,
    超时(疑似进程崩溃)可接管
  - 有 retry_at = 上次分类失败进入冷却;retry_at 前不重试,过后可重试
- --reclassify / 回填脚本走 classify_group 同步路径,不经过本锁,
  天然支持强制重打
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.message import Base, _utcnow


class TopicClassifyLock(Base):
    __tablename__ = "topic_classify_locks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(String(128), nullable=False)
    root_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # pending=正在分类/排队; done=已分类完成
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # 最近一次调度接管/更新的时刻,用于判断在途租约是否过期(疑似崩溃)
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    # LLM 失败冷却:此时间之前不重试;空=无失败记录
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "chat_id", "root_message_id", name="uq_topic_classify_chat_root"
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<TopicClassifyLock {self.chat_id}/{self.root_message_id} "
            f"status={self.status}>"
        )
