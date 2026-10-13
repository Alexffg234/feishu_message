"""Web 路由层。

包含:
- GET /           系统统计
- GET /health     存活探针(Deployment 健康检查)
- GET /ready     就绪探针(数据库连通)
- POST /feishu/event  飞书事件回调(桥接到 lark-oapi dispatcher)
- GET /messages       最近消息(查询演示)
- GET /messages/groups  按回复链分组的消息(查询演示)
- GET /messages/topics  按 LLM 主题聚合的消息(主题→回复链组)
- GET /summary           总结可视化页面(内建 HTML)
- GET /api/summary/{conversation_id}  某对话的 LLM 总结 + 完整消息树(JSON)
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from lark_oapi.core.const import UTF_8
from lark_oapi.core.model import RawRequest, RawResponse

from app.api.summary_page import SUMMARY_PAGE_HTML
from app.core.config import get_settings
from app.core.db import check_db_connection, get_session
from app.dao.conversation_repo import ConversationRepo
from app.dao.message_repo import MessageRepo
from app.schemas.message import MessageGroupOut, MessageOut, StatsOut, TopicOut
from app.services.message_service import MessageService
from app.services.summary_service import get_summary_service

logger = logging.getLogger(__name__)

# dispatcher 在应用工厂中注入
dispatcher = None  # 由 create_app 设置


router = APIRouter()


def _parse_req(request: Request, body: bytes) -> RawRequest:
    """FastAPI 请求 -> lark-oapi RawRequest(与官方 Flask 适配器对齐)。"""
    headers = {k: v for k, v in request.headers.items()}
    req = RawRequest()
    req.uri = request.url.path
    req.body = body
    req.headers = headers
    return req


def _parse_resp(raw: RawResponse) -> Response:
    return Response(
        content=raw.content or b"",
        status_code=raw.status_code or 200,
        media_type="application/json",
    )


@router.get("/", response_model=StatsOut)
def index(db: Session = Depends(get_session)) -> StatsOut:
    settings = get_settings()
    count = MessageRepo(db).count()
    return StatsOut(
        service="feishu-group-message-capture",
        status="running",
        messages_count=count,
        event_path=settings.event_path,
        database=_db_brief(settings.database_url),
        configured=settings.is_ready,
    )


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/ready")
def ready() -> JSONResponse:
    ok = check_db_connection()
    return JSONResponse(
        content={"status": "ready" if ok else "unavailable"},
        status_code=200 if ok else 503,
    )


@router.get("/messages", response_model=list[MessageOut])
def messages(limit: int = 50, db: Session = Depends(get_session)) -> list[MessageOut]:
    return MessageRepo(db).list_recent(limit=min(limit, 200))


@router.get("/messages/groups", response_model=list[MessageGroupOut])
def message_groups(
    chat_id: Optional[str] = None,
    limit: int = 20,
    db: Session = Depends(get_session),
) -> list[MessageGroupOut]:
    """按回复链分组:A 发消息、B 回复 A 归为同一组。

    - chat_id 可选,过滤单个群
    - limit 返回最近 N 个组
    """
    service = MessageService(db)
    return service.list_groups(chat_id=chat_id, limit=min(limit, 100))


@router.get("/messages/topics", response_model=list[TopicOut])
def message_topics(
    chat_id: Optional[str] = None,
    limit: int = 20,
) -> list[TopicOut]:
    """按 LLM 主题聚合:主题 → 回复链组 → 消息。

    - chat_id 可选,过滤单个群
    - limit 返回最近活跃 N 个主题
    """
    from app.services.topic_service import get_topic_service

    return get_topic_service().list_topics(chat_id=chat_id, limit=min(limit, 100))


@router.get("/summary", response_class=HTMLResponse)
def summary_page() -> HTMLResponse:
    """总结可视化页面(内建 HTML,浏览器直接访问)。"""
    return HTMLResponse(content=SUMMARY_PAGE_HTML)


@router.get("/api/conversations")
def api_conversations(
    limit: int = 30, db: Session = Depends(get_session)
) -> list[dict]:
    """返回最近的对话列表(供页面顶部切换)。"""
    return [
        {
            "id": c.id,
            "chat_id": c.chat_id,
            "root_message_id": c.root_message_id,
            "topic": c.topic,
            "message_count": c.message_count,
            "participant_count": c.participant_count,
        }
        for c in ConversationRepo(db).list_recent(limit=min(limit, 100))
    ]


@router.get("/api/summary/{conversation_id}")
def api_summary(
    conversation_id: int, db: Session = Depends(get_session)
) -> dict:
    """某对话的 LLM 总结 + 完整消息树。

    返回 {conversation_id, enabled, topic, summary, tree, messages}。
    - enabled=false 表示服务器未配置 LLM,summary 为空
    - 未找到对话返回 404
    """
    repo = ConversationRepo(db)
    from app.models.conversation import Conversation

    conv = db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="对话不存在")

    tree = repo.list_tree(conversation_id)
    summary = get_summary_service().summarize(conversation_id)

    return {
        "conversation_id": conversation_id,
        "chat_id": conv.chat_id,
        "root_message_id": conv.root_message_id,
        "topic": conv.topic or summary.get("topic", ""),
        "enabled": summary.get("enabled", False),
        "summary": summary.get("summary", ""),
        "tree": tree["roots"],
        "message_count": tree["message_count"],
    }


@router.post(get_settings().event_path)
async def feishu_event(request: Request) -> Response:
    """飞书事件回调入口。校验/解密/路由由 lark-oapi dispatcher 完成。"""
    if dispatcher is None:
        logger.error("dispatcher 未初始化")
        return JSONResponse({"msg": "dispatcher not ready"}, status_code=500)
    body = await request.body()
    req = _parse_req(request, body)
    raw = dispatcher.do(req)
    return _parse_resp(raw)


def _db_brief(url: str) -> str:
    if url.startswith("postgresql"):
        return "postgresql"
    if url.startswith("mysql") or url.startswith("mysql+pymysql"):
        return "mysql"
    if url.startswith("sqlite"):
        return "sqlite"
    return "unknown"
