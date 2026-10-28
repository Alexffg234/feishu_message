"""登录注册接口与全站访问控制中间件。

接口:
- GET  /login                 登录/注册页面(HTML)
- GET  /api/auth/register     自助注册是否开放(供页面决定是否显示注册入口)
- POST /api/auth/register     注册(成功后直接下发会话)
- POST /api/auth/login        登录
- POST /api/auth/logout       登出(清 cookie)
- GET  /api/auth/me           当前登录用户

访问控制:
install_auth_middleware(app) 注册一个 HTTP 中间件,白名单之外的一切请求
都必须携带有效会话:
- 页面请求(非 /api/*)未登录 → 302 到 /login?next=<原路径>
- 接口请求(/api/*)未登录 → 401 JSON

白名单必须包含飞书事件回调(/feishu/event),否则飞书推送会被 401 拒绝、
消息采集中断;健康检查 /health、/ready 也放行,便于容器探活。
"""
from __future__ import annotations

import logging
import time
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import STATIC_DIR, get_settings
from app.core.db import SessionLocal, get_session
from app.models.user import User
from app.services import auth_service

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------- 入参
class CredentialsIn(BaseModel):
    username: str = Field(default="", min_length=0, max_length=64)
    password: str = Field(default="", min_length=0, max_length=128)


class RegisterIn(CredentialsIn):
    display_name: str = Field(default="", max_length=64)


# ---------------------------------------------------------------- 会话
def _set_session_cookie(response: Response, user: User) -> None:
    """下发签名会话 cookie。"""
    settings = get_settings()
    token = auth_service.sign_session(user, settings.session_ttl_hours * 3600)
    response.set_cookie(
        key=auth_service.COOKIE_NAME,
        value=token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=auth_service.COOKIE_NAME, path="/")


def get_current_user(request: Request, db: Session = Depends(get_session)) -> User:
    """依赖:从 cookie 解析当前用户。未登录抛 401。"""
    user = resolve_user_from_request(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return user


def resolve_user_from_request(request: Request, db: Session) -> User | None:
    """解析请求中的会话;无效/停用/已删除均返回 None(不抛异常)。"""
    token = request.cookies.get(auth_service.COOKIE_NAME)
    if not token:
        return None
    payload = auth_service.parse_session(token)
    if not payload:
        return None
    user = auth_service.get_user_by_id(db, int(payload.get("uid", 0)))
    if user is None or not user.is_active:
        return None
    # 用户名变更/删号后会话里的名字不一致时以库为准,不额外校验
    return user


# ---------------------------------------------------------------- 页面
@router.get("/login", response_class=HTMLResponse)
def login_page() -> HTMLResponse:
    """登录/注册页。静态文件在 app/static/,便于浏览器缓存与独立维护。"""
    page = STATIC_DIR / "login.html"
    if not page.is_file():
        logger.error("登录页静态文件缺失: %s", page)
        raise HTTPException(status_code=500, detail="登录页资源缺失")
    return FileResponse(page, media_type="text/html; charset=utf-8")


# ---------------------------------------------------------------- 接口
@router.get("/api/auth/register")
def register_status(db: Session = Depends(get_session)) -> dict:
    """自助注册是否开放 + 密码复杂度规则(供页面渲染规则清单,保证前后端口径一致)。

    库里无账号时恒为开放(否则无法创建首个管理员)。
    """
    settings = get_settings()
    empty = auth_service.count_users(db) == 0
    return {
        "open": bool(settings.allow_public_register or empty),
        "password_rules": list(auth_service.PASSWORD_RULES),
        "min_len": auth_service.MIN_PASSWORD_LEN,
        "max_len": auth_service.MAX_PASSWORD_LEN,
    }


@router.post("/api/auth/register")
def register(body: RegisterIn, db: Session = Depends(get_session)) -> JSONResponse:
    if not register_status(db)["open"]:
        raise HTTPException(status_code=403, detail="自助注册已关闭,请联系管理员开通账号")
    try:
        user = auth_service.create_user(
            db, body.username, body.password, display_name=body.display_name
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    data = {"id": user.id, "username": user.username, "role": user.role}
    resp = JSONResponse(content=data)
    _set_session_cookie(resp, user)
    logger.info("账号注册成功 %s", user.username)
    return resp


@router.post("/api/auth/login")
def login(body: CredentialsIn, request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    throttle = auth_service.get_throttle()
    key = (body.username or "").strip().lower()
    if throttle.locked(key):
        raise HTTPException(status_code=429, detail="登录失败次数过多,请 5 分钟后再试")

    user = auth_service.authenticate(db, body.username, body.password)
    if user is None:
        # 只对真实存在的账号计数,避免用限流探测用户名是否存在
        if auth_service.get_user_by_username(db, body.username) is not None:
            throttle.record_fail(key)
        logger.info("登录失败 username=%s ip=%s", body.username, request.client.host if request.client else "-")
        # 统一提示,不区分「用户名不存在」与「密码错误」
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    throttle.reset(key)
    resp = JSONResponse(
        content={
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name or user.username,
            "role": user.role,
        }
    )
    _set_session_cookie(resp, user)
    logger.info("登录成功 %s", user.username)
    return resp


@router.get("/logout")
def logout_page() -> RedirectResponse:
    """浏览器直接访问 /logout 也能登出(便于无入口时手动退出)。"""
    resp = RedirectResponse(url="/login", status_code=302)
    _clear_session_cookie(resp)
    return resp


@router.post("/api/auth/logout")
def logout() -> JSONResponse:
    resp = JSONResponse(content={"ok": True})
    _clear_session_cookie(resp)
    return resp


@router.get("/api/auth/me")
def me(request: Request, user: User = Depends(get_current_user)) -> dict:
    """当前登录用户 + 会话剩余有效期(供前端显示登录状态/倒计时)。"""
    settings = get_settings()
    expires_in = 0
    payload = auth_service.parse_session(
        request.cookies.get(auth_service.COOKIE_NAME, "")
    )
    if payload:
        expires_in = max(0, int(payload.get("exp", 0)) - int(time.time()))
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name or user.username,
        "role": user.role,
        "expires_in": expires_in,
        "idle_timeout": int(settings.session_idle_timeout_minutes) * 60,
    }


# ---------------------------------------------------------------- 访问控制
def _public_paths() -> set[str]:
    """无需登录即可访问的路径。"""
    settings = get_settings()
    return {
        "/login",
        "/logout",
        "/health",
        "/healthz",
        "/ready",
        "/favicon.ico",
        settings.event_path,  # 飞书事件回调:无 cookie,必须放行
    }


def _slide_session(request: Request, response: Response, token: str) -> None:
    """滑动续期会话:把 cookie 的 exp 推后一个空闲窗口(受绝对有效期封顶)。

    静默失败(不抛异常):续期失败仅意味着用户更早需要重新登录。
    """
    settings = get_settings()
    idle = int(settings.session_idle_timeout_minutes or 0) * 60
    if idle <= 0:
        return  # 0 = 关闭滑动续期,仅按 session_ttl_hours 到期
    try:
        payload = auth_service.parse_session(token)
        new_token = auth_service.renew_session(
            payload or {}, idle, int(settings.session_ttl_hours) * 3600
        )
        if not new_token or new_token == token:
            return
        response.set_cookie(
            key=auth_service.COOKIE_NAME,
            value=new_token,
            max_age=idle,
            httponly=True,
            samesite="lax",
            secure=settings.cookie_secure,
            path="/",
        )
    except Exception as exc:  # noqa: BLE001 - 续期失败绝不影响正常响应
        logger.warning("会话续期失败(忽略): %s", exc)


def install_auth_middleware(app) -> None:
    """给 FastAPI 应用装上全站登录校验。AUTH_ENABLED=false 时直接放行。"""

    @app.middleware("http")
    async def _auth_gate(request: Request, call_next):
        settings = get_settings()
        if not settings.auth_enabled:
            return await call_next(request)

        path = request.url.path
        # 白名单:登录页/登出/认证接口/健康检查/飞书回调/静态资源(CSS·JS)
        if (
            path in _public_paths()
            or path.startswith("/api/auth/")
            or path.startswith("/static/")
        ):
            return await call_next(request)

        token = request.cookies.get(auth_service.COOKIE_NAME)
        session = None
        try:
            if token:
                session = SessionLocal()
                user = resolve_user_from_request(request, session)
                if user is not None:
                    request.state.user = user
                    response = await call_next(request)
                    # 滑动续期:每次有效访问把空闲窗口往后推。
                    # 连续 SESSION_IDLE_TIMEOUT_MINUTES 无请求 → cookie 自然过期 → 需重新登录。
                    _slide_session(request, response, token)
                    return response
        finally:
            if session is not None:
                session.close()

        if path.startswith("/api/"):
            return JSONResponse(
                content={"detail": "未登录或登录已过期"}, status_code=401
            )
        target = "/login"
        if path != "/":
            target = f"/login?next={quote(path, safe='')}"
        return RedirectResponse(url=target, status_code=302)


