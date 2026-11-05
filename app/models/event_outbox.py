"""事件出站(outbox)持久化表。

event_outbox:保证飞书事件「at-least-once」。回调 submit 时先原子写入本表
(落库后才回 200),单消费者线程从本表取 pending 事件处理,成功后再标 done。
worker 崩溃 / gunicorn 优雅重启时,未处理事件仍留在表中,重启后恢复处理,
不再因纯内存队列丢失。

- event_key 唯一键:优先事件自带 id(header.event_id),飞书消息事件回退用
  (chat_id, message_id),与消息入库幂等键一致,天然防重推。
- 状态机:
  pending(已入库待处理) → processing(单消费者取走、处理中,带处理租约)
  → done(成功) / retrying(失败退避重试) / failed(重试耗尽写死信)
- retry_at_ms 兼作两用:
  processing 的「处理租约」到期(疑似持有方崩溃后可回收);
  retrying 的「下次可重试时间」。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.message import Base, BigIntPK, _utcnow


class EventOutbox(Base):
    __tablename__ = "event_outbox"

    # BigIntPK:PG/MySQL 用 BIGINT,SQLite 落到 INTEGER(否则 SQLite 的
    # BIGINT 主键不会自增,插入时 NOT NULL 报错)
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    # 去重键(事件 id 或 chat_id+message_id),唯一防重推
    event_key: Mapped[str] = mapped_column(
        String(256), nullable=False, unique=True, index=True
    )
    # 序列化后的事件(JSON),消费端反序列化后交给 process_fn
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    # pending / processing / retrying / done / failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # 已尝试次数(用于退避上限)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 下次可处理/可回收的毫秒时间戳(0=立即可处理)
    retry_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<EventOutbox {self.event_key} status={self.status}>"
