"""用户姓名缓存 ORM 模型。

飞书回调里消息 sender_id 存的是 open_id(如 ou_xxx),不是真实姓名。
本表缓存 open_id → 真实姓名(name),由 UserNameService 惰性填充:
首次遇到某发送者时调飞书通讯录接口反查一次并入库,之后直接读缓存,避免频繁调飞书。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Integer, String

from sqlalchemy.orm import Mapped, mapped_column

from app.models.message import Base, BigIntPK


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UserName(Base):
    __tablename__ = "user_names"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    # 发送者标识(open_id 为主);唯一
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    # 真实姓名(如 "王国浩");通讯录能查到才有值,查不到为空
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    # 反查失败/未授权标记(1=失败过,避免每次请求都再打飞书)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(
        String(64), nullable=False, default=lambda: _utcnow().isoformat()
    )
    updated_at: Mapped[str] = mapped_column(
        String(64), nullable=False, default=lambda: _utcnow().isoformat()
    )
