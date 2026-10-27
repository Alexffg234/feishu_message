"""用户账号模型(登录/注册与访问控制)。

设计要点:
- 用户名唯一(大小写不敏感:落库保留原样,查询统一 lower 比对)
- 只存密码摘要,绝不存明文;格式见 auth_service.hash_password
- role: admin(首个注册者)/ member,为后续权限扩展预留
- is_active: 停用账号保留数据但禁止登录(不删除,避免外键/审计断链)
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.message import BigIntPK, Base, _utcnow


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    # 登录名(唯一,3~32 字符)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    # 归一化后的登录名(小写),唯一索引由代码层保证比对一致
    username_lower: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    # 展示名(可空,默认用 username)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # 密码摘要,格式: pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")
    is_active: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.username} role={self.role}>"
