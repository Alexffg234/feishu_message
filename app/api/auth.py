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
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.login_page import LOGIN_PAGE_HTML
from app.core.config import get_settings
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
    return HTMLResponse(content=LOGIN_PAGE_HTML)


# ---------------------------------------------------------------- 接口
@router.get("/api/auth/register")
def register_status(db: Session = Depends(get_session)) -> dict:
    """自助注册是否开放。库里无账号时恒为开放(否则无法创建首个管理员)。"""
    settings = get_settings()
    empty = auth_service.count_users(db) == 0
    return {"open": bool(settings.allow_public_register or empty)}


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
def me(user: User = Depends(get_current_user)) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name or user.username,
        "role": user.role,
    }


# ---------------------------------------------------------------- 访问控制
def _public_paths() -> set[str]:
    """无需登录即可访问的路径。"""
    settings = get_settings()
    return {
        "/login",
        "/logout",
        "/health",
        "/ready",
        "/favicon.ico",
        settings.event_path,  # 飞书事件回调:无 cookie,必须放行
    }


def install_auth_middleware(app) -> None:
    """给 FastAPI 应用装上全站登录校验。AUTH_ENABLED=false 时直接放行。"""

    @app.middleware("http")
    async def _auth_gate(request: Request, call_next):
        settings = get_settings()
        if not settings.auth_enabled:
            return await call_next(request)

        path = request.url.path
        if path in _public_paths() or path.startswith("/api/auth/"):
            return await call_next(request)

        token = request.cookies.get(auth_service.COOKIE_NAME)
        session = None
        try:
            if token:
                session = SessionLocal()
                user = resolve_user_from_request(request, session)
                if user is not None:
                    request.state.user = user
                    return await call_next(request)
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


# ---------------------------------------------------------------- 页面增强
_LOGOUT_SNIPPET = """
<div id="__authbar" style="position:fixed;top:16px;right:20px;z-index:9999;display:flex;
  align-items:center;gap:10px;font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif">
  <span id="__authuser" style="font-size:12.5px;color:#6b7280"></span>
  <button id="__logout" type="button" style="padding:6px 14px;font-size:13px;cursor:pointer;
    border:1px solid #e3e6ea;border-radius:8px;background:#fff;color:#185fa5">退出登录</button>
</div>
<script>
(function(){
  var bar=document.getElementById('__authbar');
  fetch('/api/auth/me').then(function(r){return r.ok?r.json():null}).then(function(u){
    if(!u){bar.style.display='none';return;}
    document.getElementById('__authuser').textContent=u.display_name||u.username;
  }).catch(function(){bar.style.display='none'});
  document.getElementById('__logout').onclick=function(){
    fetch('/api/auth/logout',{method:'POST'}).then(function(){location.assign('/login');});
  };
})();
</script>
"""


def with_logout_button(html: str) -> str:
    """在现有页面 HTML 末尾注入右上角「退出登录」入口。

    避免改动 summary_page / habits_page 源文件;AUTH_ENABLED=false 时原样返回。
    """
    if not get_settings().auth_enabled:
        return html
    if "</body>" in html:
        return html.replace("</body>", _LOGOUT_SNIPPET + "</body>", 1)
    return html + _LOGOUT_SNIPPET
