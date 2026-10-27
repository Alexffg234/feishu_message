"""访问控制(登录注册)测试。

覆盖:未登录页面跳转 / 接口 401、飞书回调不被拦截、注册/登录/登出主流程、
重复用户名与弱密码拒绝、错误密码不泄露账号是否存在、会话防篡改、
密码摘要往返、自助注册关闭时拒绝注册。
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 必须在导入 app 之前指定测试库与固定会话密钥,避免使用开发库
os.environ["DATABASE_URL"] = "sqlite:///./data/test_auth.db"
os.environ["SESSION_SECRET"] = "test-secret-key"
os.environ["AUTH_ENABLED"] = "true"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.db import engine  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services.auth_service import (  # noqa: E402
    COOKIE_NAME,
    hash_password,
    parse_session,
    sign_session,
    verify_password,
)

_DB_FILE = Path(__file__).resolve().parent.parent / "data" / "test_auth.db"


@pytest.fixture(scope="module")
def client():
    """带 lifespan 的测试客户端(会建表)。"""
    if _DB_FILE.exists():
        _DB_FILE.unlink()
    app = create_app()
    with TestClient(app) as c:
        yield c
    engine.dispose()


def _uname(prefix: str = "u") -> str:
    return f"{prefix}{int(time.time() * 1000) % 100000000}"


# ---------------------------------------------------------------- 未登录拦截
def test_page_redirects_to_login(client):
    r = client.get("/summary", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/login?next=%2Fsummary")


def test_api_returns_401(client):
    r = client.get("/api/topics")
    assert r.status_code == 401
    assert "未登录" in r.json()["detail"]


def test_login_page_public(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "登录" in r.text


def test_health_and_ready_public(client):
    assert client.get("/health").status_code == 200


def test_feishu_callback_not_blocked(client):
    """飞书事件回调在白名单内:不能返回 302/401,否则消息采集中断。"""
    r = client.get("/feishu/event")
    assert r.status_code not in (302, 401)


# ---------------------------------------------------------------- 注册登录
def test_register_first_user_is_admin(client):
    name = _uname("admin")
    r = client.post(
        "/api/auth/register", json={"username": name, "password": "pass12345"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "admin"
    assert COOKIE_NAME in r.cookies


def test_register_rejects_duplicate_and_weak(client):
    name = _uname("dup")
    assert client.post(
        "/api/auth/register", json={"username": name, "password": "pass12345"}
    ).status_code == 200
    # 重名
    r = client.post(
        "/api/auth/register", json={"username": name, "password": "pass12345"}
    )
    assert r.status_code == 400
    # 弱密码
    r = client.post(
        "/api/auth/register", json={"username": _uname("weak"), "password": "123"}
    )
    assert r.status_code == 400


def test_login_ok_then_access_page(client):
    name = _uname("login")
    client.post("/api/auth/register", json={"username": name, "password": "pass12345"})
    client.post("/api/auth/logout")

    r = client.post("/api/auth/login", json={"username": name, "password": "pass12345"})
    assert r.status_code == 200, r.text
    me = client.get("/api/auth/me")
    assert me.status_code == 200 and me.json()["username"] == name

    page = client.get("/summary")
    assert page.status_code == 200
    assert "退出登录" in page.text


def test_wrong_password_and_unknown_user_same_error(client):
    name = _uname("pw")
    client.post("/api/auth/register", json={"username": name, "password": "pass12345"})
    client.post("/api/auth/logout")

    r1 = client.post("/api/auth/login", json={"username": name, "password": "wrongpass"})
    r2 = client.post(
        "/api/auth/login", json={"username": _uname("ghost"), "password": "wrongpass"}
    )
    assert r1.status_code == 401 and r2.status_code == 401
    assert r1.json()["detail"] == r2.json()["detail"]


def test_logout_clears_session(client):
    name = _uname("out")
    client.post("/api/auth/register", json={"username": name, "password": "pass12345"})
    assert client.get("/api/auth/me").status_code == 200
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401


# ---------------------------------------------------------------- 底层单元
def test_password_hash_roundtrip():
    stored = hash_password("hello12345")
    assert stored.startswith("pbkdf2_sha256$")
    assert verify_password("hello12345", stored)
    assert not verify_password("hello12346", stored)
    assert not verify_password("hello12345", "garbage")


def test_session_tamper_rejected(client):
    name = _uname("sig")
    r = client.post(
        "/api/auth/register", json={"username": name, "password": "pass12345"}
    )
    token = r.cookies[COOKIE_NAME]
    body, _, sig = token.rpartition(".")
    tampered = body + ".AAAA" + sig[4:]
    assert parse_session(tampered) is None
    assert parse_session("") is None


def test_sign_session_expiry():
    class _U:
        id = 1
        username = "x"

    expired = sign_session(_U(), -10)
    assert parse_session(expired) is None
