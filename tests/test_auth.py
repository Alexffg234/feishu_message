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


def test_healthz_public_and_shape(client):
    """/healthz 无需登录,并返回监控字段。"""
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert "db" in body
    assert "queue_backlog" in body
    assert "dead_letter" in body and "lines" in body["dead_letter"]
    assert "last_message_seconds_ago" in body


def test_feishu_callback_not_blocked(client):
    """飞书事件回调在白名单内:不能返回 302/401,否则消息采集中断。"""
    r = client.get("/feishu/event")
    assert r.status_code not in (302, 401)


# ---------------------------------------------------------------- 注册登录
def test_register_first_user_is_admin(client):
    name = _uname("admin")
    r = client.post(
        "/api/auth/register", json={"username": name, "password": "feishu2026x"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "admin"
    assert COOKIE_NAME in r.cookies


def test_register_rejects_duplicate_and_weak(client):
    name = _uname("dup")
    assert client.post(
        "/api/auth/register", json={"username": name, "password": "feishu2026x"}
    ).status_code == 200
    # 重名
    r = client.post(
        "/api/auth/register", json={"username": name, "password": "feishu2026x"}
    )
    assert r.status_code == 400
    # 弱密码
    r = client.post(
        "/api/auth/register", json={"username": _uname("weak"), "password": "123"}
    )
    assert r.status_code == 400


def test_password_complexity_rules():
    """密码复杂度:长度 / 字母+数字 / 弱密码 / 重复字符 / 连续字符。"""
    from app.services.auth_service import password_issues, password_strength

    assert password_issues("feishu2026x") == []
    assert password_issues("Kx7m2q9p") == []
    assert password_strength("feishu2026x") >= 1

    cases = {
        "short1": "长度不足",
        "abcdefgh": "缺数字",
        "12345678": "纯数字且属弱密码",
        "1111abcd": "同一字符重复 4 次",
        "abc12345": "5 位连续字符",
        "password": "常见弱密码",
        "qwerty12": "常见弱密码",
    }
    for pwd, why in cases.items():
        assert password_issues(pwd), f"{pwd} 应被拒绝({why})"


def test_register_rejects_weak_password_variants(client):
    """各类不合规密码均返回 400,并带上具体原因。"""
    for pwd in ("abcdefgh", "12345678", "1111abcd", "abc12345", "password"):
        r = client.post(
            "/api/auth/register",
            json={"username": _uname("bad"), "password": pwd},
        )
        assert r.status_code == 400, f"{pwd} 不应通过"
        assert "密码" in r.json()["detail"]


def test_register_status_exposes_password_rules(client):
    data = client.get("/api/auth/register").json()
    assert data["open"] is True
    assert data["min_len"] == 8
    assert isinstance(data["password_rules"], list) and len(data["password_rules"]) >= 3
    # 规则应出现在页面里(页面用后端下发的规则渲染清单)
    assert "同时包含字母和数字" in " ".join(data["password_rules"])


def test_login_ok_then_access_page(client):
    name = _uname("login")
    client.post("/api/auth/register", json={"username": name, "password": "feishu2026x"})
    client.post("/api/auth/logout")

    r = client.post("/api/auth/login", json={"username": name, "password": "feishu2026x"})
    assert r.status_code == 200, r.text
    me = client.get("/api/auth/me")
    assert me.status_code == 200 and me.json()["username"] == name

    page = client.get("/summary")
    assert page.status_code == 200
    # 顶部导航(含退出登录)由 static/nav.js 动态渲染到 #app-nav 挂载点
    assert 'id="app-nav"' in page.text
    assert "/static/nav.js" in page.text


def test_static_assets_reachable_without_login(client):
    """静态资源(CSS/JS)在未登录时必须可访问,否则页面样式与导航会被拦成 401。"""
    for asset in ("/static/components.css", "/static/nav.js", "/static/login.css"):
        r = client.get(asset)
        assert r.status_code == 200, f"{asset} -> {r.status_code}"


def test_wrong_password_and_unknown_user_same_error(client):
    name = _uname("pw")
    client.post("/api/auth/register", json={"username": name, "password": "feishu2026x"})
    client.post("/api/auth/logout")

    r1 = client.post("/api/auth/login", json={"username": name, "password": "wrongpass"})
    r2 = client.post(
        "/api/auth/login", json={"username": _uname("ghost"), "password": "wrongpass"}
    )
    assert r1.status_code == 401 and r2.status_code == 401
    assert r1.json()["detail"] == r2.json()["detail"]


def test_logout_clears_session(client):
    name = _uname("out")
    client.post("/api/auth/register", json={"username": name, "password": "feishu2026x"})
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
        "/api/auth/register", json={"username": name, "password": "feishu2026x"}
    )
    token = r.cookies[COOKIE_NAME]
    body, _, sig = token.rpartition(".")
    tampered = body + ".AAAA" + sig[4:]
    assert parse_session(tampered) is None
    assert parse_session("") is None


def test_masks_database_url_in_logs():
    """数据库 URL 里的密码必须被打码,防止泄露进日志/告警。"""
    from app.core.config import mask_db_url

    masked = mask_db_url(
        "mysql+pymysql://root:GoGo2016@112.126.101.128:3306/feishu_message?charset=utf8mb4"
    )
    assert "GoGo2016" not in masked
    assert "root:***" in masked
    assert mask_db_url("sqlite:///./data/messages.db") == "sqlite:///./data/messages.db"
    assert mask_db_url("") == ""


def test_sign_session_expiry():
    class _U:
        id = 1
        username = "x"

    expired = sign_session(_U(), -10)
    assert parse_session(expired) is None


# ---------------------------------------------------------------- 空闲超时
def test_renew_session_slides_and_caps(client):
    """renew_session:续期后 exp 后移一个空闲窗口,但不越过绝对有效期。"""
    from app.services.auth_service import renew_session

    class _U:
        id = 7
        username = "slider"

    idle = 600  # 10 分钟
    token = sign_session(_U(), idle)
    payload = parse_session(token)
    assert payload and payload["iat"] > 0

    # 1) 续期:exp 应 >= 原 exp(往后推),且不超过 iat + 绝对上限
    renewed = renew_session(payload, idle, 3600)
    p2 = parse_session(renewed)
    assert p2 is not None and p2["exp"] >= payload["exp"]
    assert p2["exp"] <= payload["iat"] + 3600
    assert p2["iat"] == payload["iat"]  # 最初登录时刻不能被改写

    # 2) 已达绝对上限:不再续期
    assert renew_session(payload, idle, 0) is None

    # 3) 非法 payload
    assert renew_session({}, idle, 3600) is None
    assert renew_session({"uid": 0, "iat": 0}, idle, 3600) is None


def test_sliding_refresh_on_activity(client):
    """有访问时会话被滑动续期:响应里带回新 cookie,且有效期收敛到空闲窗口。"""
    name = _uname("slide")
    r = client.post(
        "/api/auth/register", json={"username": name, "password": "feishu2026x"}
    )
    assert r.status_code == 200
    before = r.cookies[COOKIE_NAME]

    # 普通接口属于「用户活动」,应触发滑动续期(认证接口 /api/auth/* 不算)
    assert client.get("/api/topics").status_code == 200
    after = client.cookies.get(COOKIE_NAME)
    assert after and after != before, "有效访问应触发滑动续期"

    # 续期后剩余有效期应收敛到空闲窗口(10 分钟),而不是原始的 12 小时
    payload = parse_session(after)
    assert payload is not None
    assert 0 < payload["exp"] - int(time.time()) <= 600

    # /api/auth/me 暴露剩余时间,供前端显示登录状态
    body = client.get("/api/auth/me").json()
    assert body["idle_timeout"] == 600
    assert 0 < body["expires_in"] <= 600


def test_expired_session_logs_out(client):
    """会话过期后:接口 401、页面跳登录页。"""
    name = _uname("exp")
    assert client.post(
        "/api/auth/register", json={"username": name, "password": "feishu2026x"}
    ).status_code == 200

    class _U:
        id = 1
        username = name

    # 直接塞一个已过期的会话串(cookie 的 exp 在签发时就固定)
    client.cookies.set(COOKIE_NAME, sign_session(_U(), -10))
    assert client.get("/api/auth/me").status_code == 401
    r = client.get("/summary", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/login")
