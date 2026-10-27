"""账号认证服务:密码摘要、账号 CRUD、会话签名与登录限流。

设计取舍(与项目现状匹配):
- 零新增依赖:密码摘要用标准库 hashlib.pbkdf2_hmac(SHA256 + 每账号随机盐),
  会话用 HMAC-SHA256 签名 cookie。不需要 passlib / bcrypt / python-jose。
- 无状态会话:不建 sessions 表,登出即清 cookie;密钥持久化在 data/ 下,
  保证服务重启后已登录用户不会被强制下线。
- 登录限流:进程内滑动计数(多 worker 下各进程独立,仅缓解暴力破解,
  真实防护应放在 nginx / 网关层)。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from functools import lru_cache

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import BASE_DIR, get_settings
from app.models.user import User

logger = logging.getLogger(__name__)

# Cookie 名与会话默认值
COOKIE_NAME = "fs_session"
_SESSION_SECRET_FILE = BASE_DIR / "data" / ".session_secret"

# 密码摘要参数(PBKDF2 迭代次数,OWASP 对 SHA256 的建议量级)
_PBKDF2_ITERATIONS = 200_000
_SALT_BYTES = 16

# 账号/密码规则
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-一-龥]{3,32}$")
MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 128

# 登录失败限流
_MAX_FAILS = 5
_FAIL_WINDOW_SEC = 300


# ---------------------------------------------------------------- 密码摘要
def hash_password(password: str) -> str:
    """生成密码摘要。格式 pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>。"""
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码;摘要格式损坏/算法变更时返回 False(不抛异常)。"""
    if not stored or not password:
        return False
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return False
    try:
        iterations = int(parts[1])
        salt = bytes.fromhex(parts[2])
        expected = bytes.fromhex(parts[3])
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    # 定长时间比较,防时序侧信道
    return hmac.compare_digest(dk, expected)


# ---------------------------------------------------------------- 账号校验
def validate_username(username: str) -> str:
    """校验并归一化用户名。不合法抛 ValueError。"""
    name = (username or "").strip()
    if not USERNAME_RE.match(name):
        raise ValueError("用户名需为 3~32 位字母、数字、中文或 _ . -")
    return name


def validate_password(password: str) -> str:
    """校验密码强度(当前只约束长度)。不合法抛 ValueError。"""
    pwd = password or ""
    if len(pwd) < MIN_PASSWORD_LEN:
        raise ValueError(f"密码至少 {MIN_PASSWORD_LEN} 位")
    if len(pwd) > MAX_PASSWORD_LEN:
        raise ValueError(f"密码最长 {MAX_PASSWORD_LEN} 位")
    return pwd


# ---------------------------------------------------------------- 账号 CRUD
def get_user_by_username(session: Session, username: str) -> User | None:
    """按用户名(大小写不敏感)查账号。"""
    key = (username or "").strip().lower()
    if not key:
        return None
    return session.execute(
        select(User).where(User.username_lower == key)
    ).scalar_one_or_none()


def get_user_by_id(session: Session, user_id: int) -> User | None:
    return session.get(User, user_id)


def count_users(session: Session) -> int:
    return int(session.execute(select(func.count(User.id))).scalar() or 0)


def create_user(
    session: Session,
    username: str,
    password: str,
    display_name: str = "",
    role: str | None = None,
) -> User:
    """创建账号。用户名重复抛 ValueError;首个账号自动成为 admin。

    role 为空时:库里尚无账号 → admin,否则 member。
    """
    name = validate_username(username)
    pwd = validate_password(password)
    key = name.lower()
    if get_user_by_username(session, name) is not None:
        raise ValueError("用户名已被占用")
    if role is None:
        role = "admin" if count_users(session) == 0 else "member"
    user = User(
        username=name,
        username_lower=key,
        display_name=(display_name or "").strip()[:64] or name,
        password_hash=hash_password(pwd),
        role=role,
        is_active=1,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    logger.info("创建账号 %s role=%s", name, role)
    return user


def authenticate(session: Session, username: str, password: str) -> User | None:
    """校验账号密码。成功返回 User 并刷新 last_login_at;失败返回 None。"""
    user = get_user_by_username(session, username)
    if user is None:
        # 走一次同等开销的摘要运算,避免「用户名不存在」被时序探测
        hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), b"\x00" * _SALT_BYTES, _PBKDF2_ITERATIONS
        )
        return None
    if not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    user.last_login_at = datetime.now(timezone.utc)
    session.commit()
    return user


def set_password(session: Session, user: User, new_password: str) -> None:
    """改密(供 CLI 与后续个人中心复用)。"""
    user.password_hash = hash_password(validate_password(new_password))
    session.commit()


# ---------------------------------------------------------------- 会话签名
def _load_or_create_secret() -> str:
    """会话签名密钥:配置优先,否则持久化在 data/.session_secret。

    落盘文件的目的:未显式配置时,重启后旧 cookie 仍然有效。
    """
    configured = (get_settings().session_secret or "").strip()
    if configured:
        return configured
    try:
        if _SESSION_SECRET_FILE.exists():
            existing = _SESSION_SECRET_FILE.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        _SESSION_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        generated = secrets.token_urlsafe(48)
        _SESSION_SECRET_FILE.write_text(generated, encoding="utf-8")
        try:
            os.chmod(_SESSION_SECRET_FILE, 0o600)
        except OSError:  # Windows 无 chmod 语义,忽略
            pass
        logger.info("已生成会话签名密钥: %s", _SESSION_SECRET_FILE)
        return generated
    except OSError:
        logger.exception("会话密钥读写失败,退化为进程内随机密钥(重启后需重新登录)")
        return secrets.token_urlsafe(48)


@lru_cache
def get_session_secret() -> str:
    """密钥单例(进程内缓存,避免每次请求读文件)。"""
    return _load_or_create_secret()


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def sign_session(user: User, ttl_seconds: int) -> str:
    """生成签名会话串: base64url(payload).hmac_sha256。"""
    payload = {
        "uid": int(user.id),
        "u": user.username,
        "exp": int(time.time()) + int(ttl_seconds),
    }
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(
        get_session_secret().encode("utf-8"), body.encode("utf-8"), hashlib.sha256
    ).digest()
    return f"{body}.{_b64url_encode(sig)}"


def parse_session(token: str) -> dict | None:
    """校验并解析会话串。签名/过期/格式任一不合法返回 None。"""
    if not token or "." not in token:
        return None
    body, _, sig = token.rpartition(".")
    expected = hmac.new(
        get_session_secret().encode("utf-8"), body.encode("utf-8"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(_b64url_encode(expected), sig):
        return None
    try:
        payload = json.loads(_b64url_decode(body))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload


# ---------------------------------------------------------------- 登录限流
class LoginThrottle:
    """进程内登录失败计数:同一用户名连续失败超限后冷却。"""

    def __init__(self, max_fails: int = _MAX_FAILS, window: int = _FAIL_WINDOW_SEC):
        self._max = max_fails
        self._window = window
        self._lock = threading.Lock()
        self._fails: dict[str, tuple[int, float]] = {}

    def locked(self, key: str) -> bool:
        with self._lock:
            self._evict()
            count, last = self._fails.get(key, (0, 0.0))
            if count >= self._max and time.monotonic() - last < self._window:
                return True
            return False

    def record_fail(self, key: str) -> None:
        with self._lock:
            count, _last = self._fails.get(key, (0, 0.0))
            self._fails[key] = (count + 1, time.monotonic())

    def reset(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)

    def _evict(self) -> None:
        now = time.monotonic()
        for k, (_c, last) in list(self._fails.items()):
            if now - last >= self._window:
                self._fails.pop(k, None)


_throttle = LoginThrottle()


def get_throttle() -> LoginThrottle:
    return _throttle
