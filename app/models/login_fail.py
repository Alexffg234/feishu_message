"""登录失败限流表(多 worker 共享)。

login_fails:key = 用户名(小写),记录连续登录失败次数与滑动窗口截止。
取代 auth_service.LoginThrottle 的进程内 dict,使多个 gunicorn worker
共享同一份失败计数,暴力破解不会被 n 个 worker 放大。

语义与旧进程内实现一致:连续失败 _MAX_FAILS 次后,在 _FAIL_WINDOW_SEC
内锁定(403/429);成功登录即清空计数。
"""
from __future__ import annotations

from sqlalchemy import BigInteger, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.message import Base


class LoginFail(Base):
    __tablename__ = "login_fails"

    # 用户名(小写)主键
    username: Mapped[str] = mapped_column(String(128), primary_key=True)
    fail_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 当前滑动窗口截止时刻(毫秒时间戳);超过后重计
    window_expires_at_ms: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<LoginFail {self.username} count={self.fail_count}>"
