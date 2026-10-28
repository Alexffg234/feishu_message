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
- GET /api/topics        问题分类统计(按 topic 标签聚合)
- GET /api/topics/summary  某主题的 AI 分析(读缓存)
- POST /api/topics/summary 强制重新生成某主题的 AI 分析
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from lark_oapi.core.const import UTF_8
from lark_oapi.core.model import RawRequest, RawResponse

from app.core.config import STATIC_DIR, BASE_DIR, get_settings
from app.core.db import check_db_connection, get_session
from app.dao.conversation_repo import ConversationRepo
from app.dao.message_repo import MessageRepo
from app.schemas.message import MessageGroupOut, MessageOut, StatsOut, TopicOut
from app.models.conversation import Conversation
from app.models.message import Message
from app.services.message_service import MessageService
from app.services.summary_service import get_summary_service
from app.services.user_name_service import fill_tree_sender_names
from app.services.habits_service import analyze as analyze_habits

logger = logging.getLogger(__name__)


class TopicGroupIn(BaseModel):
    """问题大类(主题分组)入参:同名 upsert,topics 整体覆盖。

    parent 可选:填另一组名则本组作为其子类(两级分组),空串=顶级大类。
    """

    name: str
    topics: list[str]
    parent: str = ""


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


@router.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    """根路径直接跳到主页面(总结页)。

    未登录时认证中间件会先拦下跳 /login;已登录就进主页面,
    而不是对着一坨统计 JSON。统计信息改由 /api/stats 提供。
    """
    return RedirectResponse(url="/summary", status_code=302)


@router.get("/api/stats", response_model=StatsOut)
def stats(db: Session = Depends(get_session)) -> StatsOut:
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


_DEAD_LETTER_FILE = BASE_DIR / "data" / "dead_events.jsonl"


@router.get("/ready")
def ready() -> JSONResponse:
    ok = check_db_connection()
    return JSONResponse(
        content={"status": "ready" if ok else "unavailable"},
        status_code=200 if ok else 503,
    )


@router.get("/healthz")
def healthz(db: Session = Depends(get_session)) -> JSONResponse:
    """可观测性探活端点:一次给出『可用性 + 数据新鲜度 + 积压 + 死信』。

    供 scripts/health_probe.py 定时拉取生成告警,也可人工查看:
    - db:            数据库连通性
    - queue_backlog: 事件队列积压(各 worker 独立,此处反映本进程队列)
    - dead_letter:   死信文件(事件处理持续失败会累积在此)大小与行数
    - last_message:  最近一条入库消息时间 + 距今秒数(数据新鲜度)
    状态: 任一关键项异常 => "degraded",否则 "ok"。
    """
    db_ok = check_db_connection()

    # 队列积压(本 worker 的进程内队列)
    backlog = 0
    try:
        from app.services.feishu_handler import get_event_queue

        backlog = get_event_queue().qsize()
    except Exception:  # noqa: BLE001 - 队列不可用不算整体故障
        backlog = -1

    # 死信文件
    dl_bytes = dl_lines = 0
    try:
        if _DEAD_LETTER_FILE.exists():
            dl_bytes = _DEAD_LETTER_FILE.stat().st_size
            with open(_DEAD_LETTER_FILE, encoding="utf-8") as f:
                dl_lines = sum(1 for _ in f)
    except OSError:
        pass

    # 最近一条消息(数据新鲜度)
    last_msg_iso = None
    last_msg_seconds = -1
    try:
        from sqlalchemy import text

        row = db.execute(
            text(
                "SELECT MAX(received_at) FROM messages "
                "UNION SELECT MAX(create_time) FROM messages "
                "ORDER BY 1 DESC LIMIT 1"
            )
        ).scalar()
        if row:
            last_msg_iso = str(row)
            # create_time 是毫秒时间戳字符串;received_at 是 ISO datetime,两种都算
            import time as _t
            from datetime import datetime as _dt

            now = _t.time()
            ts = None
            raw = str(row)
            try:
                rep = int(raw)
                ts = rep / 1000.0 if rep > 10**12 else float(rep)
            except (TypeError, ValueError):
                try:
                    ts = _dt.fromisoformat(raw[:19]).timestamp()
                except (TypeError, ValueError):
                    ts = None
            if ts is not None:
                last_msg_seconds = max(0, int(now - ts))
    except Exception:  # noqa: BLE001
        last_msg_iso = None

    bad = (not db_ok) or backlog < 0
    return JSONResponse(
        content={
            "status": "degraded" if bad else "ok",
            "db": db_ok,
            "queue_backlog": backlog,
            "dead_letter": {"bytes": dl_bytes, "lines": dl_lines},
            "last_message": last_msg_iso,
            "last_message_seconds_ago": last_msg_seconds,
        },
        status_code=200,
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
    """群聊总结可视化页面(app/static/summary.html)。"""
    return _page("summary.html")


@router.get("/api/topics")
def api_topics(limit: int = 50, db: Session = Depends(get_session)) -> dict:
    """问题分类统计(按 topic 标签聚合,供 /summary 页面顶部统计卡片)。

    - 每个 topic:案例数/消息数/人数/首次出现/最近活跃/关联对话 id
    - summary_status: ready(缓存新鲜)/ stale(有新消息待重生成)/ missing(未生成)
    """
    from sqlalchemy import func

    from app.models.topic_summary import TopicSummary

    limit = min(limit, 100)
    msg_stats = (
        db.query(
            Message.topic,
            func.count(Message.id).label("msg_count"),
            func.count(func.distinct(Message.sender_id)).label("people"),
            func.min(Message.create_time).label("first_at"),
            func.max(Message.create_time).label("last_at"),
            func.max(Message.id).label("max_id"),
        )
        .where(Message.topic != "")
        .group_by(Message.topic)
        .order_by(func.max(Message.id).desc())
        .limit(limit)
        .all()
    )
    topic_names = [r[0] for r in msg_stats]

    conv_counts = {
        r[0]: int(r[1])
        for r in db.query(
            Conversation.topic, func.count(Conversation.id)
        )
        .where(Conversation.topic != "")
        .group_by(Conversation.topic)
        .all()
    }
    ids_by_topic: dict[str, list[int]] = {}
    if topic_names:
        for cid, t in db.query(Conversation.id, Conversation.topic).where(
            Conversation.topic.in_(topic_names)
        ):
            ids_by_topic.setdefault(t, []).append(cid)
    for v in ids_by_topic.values():
        v.sort()

    cache_by_topic: dict[str, object] = {}
    if topic_names:
        for row in db.query(TopicSummary).where(TopicSummary.topic.in_(topic_names)).all():
            if row.topic not in cache_by_topic:
                cache_by_topic[row.topic] = row

    from app.models.topic_knowledge import TopicKnowledge
    from app.services.topic_summary_service import TopicSummaryService

    khash_by_topic = {
        r.topic: TopicSummaryService._knowledge_hash(r.content or "")
        for r in db.query(TopicKnowledge).all()
    }

    topics = []
    for r in msg_stats:
        t = r[0]
        max_id = int(r.max_id or 0)
        row = cache_by_topic.get(t)
        if row is None:
            summary_status = "missing"
        elif (
            row.message_count,
            int(row.max_message_id or 0),
            row.knowledge_hash or 0,
        ) == (int(r.msg_count), max_id, khash_by_topic.get(t, 0)):
            summary_status = "ready"
        else:
            summary_status = "stale"
        topics.append(
            {
                "topic": t,
                "conversation_count": conv_counts.get(t, 0),
                "message_count": int(r.msg_count),
                "participant_count": int(r.people),
                "first_at": r.first_at or "",
                "last_at": r.last_at or "",
                "conversation_ids": ids_by_topic.get(t, []),
                "summary_status": summary_status,
            }
        )

    return {
        "overall": {
            "topic_count": db.query(func.count(func.distinct(Message.topic)))
            .where(Message.topic != "")
            .scalar()
            or 0,
            "conversation_count": db.query(func.count(Conversation.id)).scalar() or 0,
            "unclassified_conversations": db.query(func.count(Conversation.id))
            .where(Conversation.topic == "")
            .scalar()
            or 0,
            "message_count": db.query(func.count(Message.id)).scalar() or 0,
        },
        "topics": topics,
    }


@router.get("/api/topics/summary")
def api_topic_summary(
    topic: str, chat_id: Optional[str] = None, db: Session = Depends(get_session)
) -> dict:
    """某主题标签的 AI 分析(纯读缓存,秒回)。

    - chat_id 省略时自动取该 topic 所属群
    - 无缓存时 found=false(后台定时任务会补齐,或手动 POST 强制生成)
    """
    from sqlalchemy import select

    from app.services.topic_summary_service import get_topic_summary_service

    if not chat_id:
        chat_id = db.execute(
            select(Message.chat_id).where(Message.topic == topic).limit(1)
        ).scalar_one_or_none()
        if chat_id is None:
            raise HTTPException(status_code=404, detail="主题不存在")
    return get_topic_summary_service().get_cached(chat_id, topic)


@router.post("/api/topics/summary")
async def api_topic_summary_regenerate(
    topic: str,
    chat_id: Optional[str] = None,
    db: Session = Depends(get_session),
) -> dict:
    """强制重新生成某主题的 AI 分析(同步,约 5~15 秒)。"""
    from sqlalchemy import select

    from app.services.topic_summary_service import get_topic_summary_service

    svc = get_topic_summary_service()
    if not svc.enabled:
        raise HTTPException(status_code=503, detail="LLM 未配置,无法生成")
    if not chat_id:
        chat_id = db.execute(
            select(Message.chat_id).where(Message.topic == topic).limit(1)
        ).scalar_one_or_none()
        if chat_id is None:
            raise HTTPException(status_code=404, detail="主题不存在")
    result = await run_in_threadpool(svc.generate_topic, chat_id, topic, True)
    if result is None:
        raise HTTPException(status_code=409, detail="消息内容不足或生成失败")
    return result


@router.get("/api/topic_groups")
def api_topic_groups_list(db: Session = Depends(get_session)) -> dict:
    """问题大类列表(主题分组,前端按大类折叠展示小类)。"""
    from app.models.topic_group import TopicGroup

    rows = db.query(TopicGroup).order_by(TopicGroup.sort_order, TopicGroup.id).all()
    return {
        "groups": [
            {
                "name": r.name,
                "topics": r.topics or [],
                "sort_order": r.sort_order,
                "parent": r.parent or "",
            }
            for r in rows
        ]
    }


@router.post("/api/topic_groups")
def api_topic_groups_upsert(body: TopicGroupIn, db: Session = Depends(get_session)) -> dict:
    """创建/更新问题大类(name 唯一,topics 整体覆盖)。

    两级分组:填 parent(父组名)则本组作为其子类;仅含子类的顶级组 topics 可为空。
    """
    from app.models.topic_group import TopicGroup

    name = (body.name or "").strip()
    parent = (body.parent or "").strip()
    topics = [t.strip() for t in body.topics if (t or "").strip()]
    if not name:
        raise HTTPException(status_code=400, detail="name 不能为空")
    row = db.query(TopicGroup).filter(TopicGroup.name == name).first()
    if row is None:
        from sqlalchemy import func

        max_order = db.query(func.max(TopicGroup.sort_order)).scalar() or 0
        row = TopicGroup(
            name=name, topics=topics, parent=parent, sort_order=int(max_order) + 1
        )
        db.add(row)
    else:
        row.topics = topics
        row.parent = parent
    db.commit()
    db.refresh(row)
    return {"name": row.name, "parent": row.parent or "", "topics": row.topics or []}


@router.delete("/api/topic_groups/{name}")
def api_topic_groups_delete(name: str, db: Session = Depends(get_session)) -> dict:
    from app.models.topic_group import TopicGroup

    row = db.query(TopicGroup).filter(TopicGroup.name == name).first()
    if row is None:
        raise HTTPException(status_code=404, detail="大类不存在")
    db.delete(row)
    db.commit()
    return {"deleted": name}


class TopicKnowledgeIn(BaseModel):
    """主题参考知识库入参:同名 upsert,整体覆盖 content。"""

    topic: str
    content: str


@router.get("/api/topic_knowledge")
def api_topic_knowledge_get(topic: str, db: Session = Depends(get_session)) -> dict:
    from app.models.topic_knowledge import TopicKnowledge

    row = db.query(TopicKnowledge).filter(TopicKnowledge.topic == topic).first()
    return {"topic": topic, "content": row.content if row else ""}


@router.post("/api/topic_knowledge")
def api_topic_knowledge_upsert(
    body: TopicKnowledgeIn, db: Session = Depends(get_session)
) -> dict:
    """写入/覆盖某 topic 的参考知识库(标准排查手册)。"""
    from datetime import datetime, timezone

    from app.models.message import _utcnow
    from app.models.topic_knowledge import TopicKnowledge

    topic = (body.topic or "").strip()
    content = (body.content or "").strip()
    if not topic or not content:
        raise HTTPException(status_code=400, detail="topic 与 content 不能为空")
    row = db.query(TopicKnowledge).filter(TopicKnowledge.topic == topic).first()
    if row is None:
        row = TopicKnowledge(topic=topic, content=content, updated_at=_utcnow())
        db.add(row)
    else:
        row.content = content
        row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return {"topic": row.topic, "length": len(row.content or "")}


@router.delete("/api/topic_knowledge/{topic}")
def api_topic_knowledge_delete(topic: str, db: Session = Depends(get_session)) -> dict:
    from app.models.topic_knowledge import TopicKnowledge

    row = db.query(TopicKnowledge).filter(TopicKnowledge.topic == topic).first()
    if row is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    db.delete(row)
    db.commit()
    return {"deleted": topic}


@router.get("/habits", response_class=HTMLResponse)
def habits_page() -> HTMLResponse:
    """对话习惯可视化页面(app/static/habits.html)。"""
    return _page("habits.html")


def _page(filename: str) -> HTMLResponse:
    """返回 app/static/ 下的页面文件(顶部导航由 static/nav.js 统一渲染)。"""
    page = STATIC_DIR / filename
    if not page.is_file():
        logger.error("页面静态文件缺失: %s", page)
        raise HTTPException(status_code=500, detail="页面资源缺失")
    return FileResponse(page, media_type="text/html; charset=utf-8")


@router.get("/api/habits")
def api_habits(
    chat_id: Optional[str] = None, limit: int = 2000, db: Session = Depends(get_session)
) -> dict:
    """对话习惯分析结构化 JSON(供 /habits 页面消费,只读)。"""
    return analyze_habits(chat_id=chat_id, limit=min(limit, 10000))


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
    # 给树节点补充发送者真实姓名(无论 LLM 是否启用,都展示人名)
    fill_tree_sender_names(tree["roots"])
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
    # dispatcher.do 做签名校验/解密(同步),回调本身只入队(先应答后处理),
    # 正常路径毫秒级返回 200;媒体下载/DB 写入由后台消费者线程处理,
    # 慢事件不再拖慢应答、不再触发 gunicorn worker 超时被杀。
    # 队列满时回调降级同步处理,故仍需线程池兜底,避免卡死事件循环。
    raw = await run_in_threadpool(dispatcher.do, req)
    return _parse_resp(raw)


def _db_brief(url: str) -> str:
    if url.startswith("postgresql"):
        return "postgresql"
    if url.startswith("mysql") or url.startswith("mysql+pymysql"):
        return "mysql"
    if url.startswith("sqlite"):
        return "sqlite"
    return "unknown"
