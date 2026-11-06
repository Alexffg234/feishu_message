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
from app.api.auth import require_admin
from app.dao.conversation_repo import ConversationRepo
from app.dao.message_repo import MessageRepo
from app.schemas.message import MessageGroupOut, MessageOut, StatsOut, TopicOut
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user import User
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
    user: User = Depends(require_admin),
) -> dict:
    """强制重新生成某主题的 AI 分析(同步,约 5~15 秒,付费 LLM 调用,仅管理员)。"""
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
def api_topic_groups_upsert(
    body: TopicGroupIn,
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
) -> dict:
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
def api_topic_groups_delete(
    name: str,
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
) -> dict:
    from app.models.topic_group import TopicGroup

    row = db.query(TopicGroup).filter(TopicGroup.name == name).first()
    if row is None:
        raise HTTPException(status_code=404, detail="大类不存在")
    db.delete(row)
    db.commit()
    return {"deleted": name}


# ---------------------------------------------------------------- 标签别名(阶段一)
@router.get("/api/topic_aliases")
def api_topic_aliases_list(db: Session = Depends(get_session)) -> dict:
    """标签别名列表(alias -> canonical 收敛表),供管理页维护。"""
    from app.models.topic_alias import TopicAlias

    rows = db.query(TopicAlias).order_by(TopicAlias.canonical, TopicAlias.alias).all()
    return {
        "aliases": [
            {"alias": r.alias, "canonical": r.canonical, "auto": r.auto}
            for r in rows
        ]
    }


@router.post("/api/topic_aliases")
def api_topic_aliases_upsert(
    body: TopicAliasIn,
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
) -> dict:
    """新建/更新一条别名收敛(同名 upsert)。alias/canonical 均做标签清洗。

    **拒绝把标准标签设为 alias**:那等于宣告该标准标签作废,以后所有它的输出
    都会被改写成另一个标签——一次手滑毒化整个标签体系,比拒绝麻烦得多。
    """
    from app.models.topic_alias import TopicAlias
    from app.services.label_norm import clean_label
    from app.services.topic_service import get_standard_labels

    alias = clean_label(body.alias)
    canonical = clean_label(body.canonical)
    if not alias or not canonical:
        raise HTTPException(status_code=400, detail="alias 与 canonical 均不能为空(语气词无效)")
    if alias == canonical:
        raise HTTPException(status_code=400, detail="alias 与 canonical 不能相同")
    if alias in set(get_standard_labels(db)):
        raise HTTPException(
            status_code=400,
            detail=f"{alias!r} 是标准标签,不能作为别名(会让该标签失效)",
        )
    row = db.query(TopicAlias).filter(TopicAlias.alias == alias).first()
    if row is None:
        row = TopicAlias(alias=alias, canonical=canonical, auto=0)
        db.add(row)
    else:
        row.canonical = canonical
        row.auto = 0
    db.commit()
    db.refresh(row)
    return {"alias": row.alias, "canonical": row.canonical, "auto": row.auto}


@router.delete("/api/topic_aliases/{alias}")
def api_topic_aliases_delete(
    alias: str,
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
) -> dict:
    """删除一条别名收敛。"""
    from app.models.topic_alias import TopicAlias

    row = db.query(TopicAlias).filter(TopicAlias.alias == alias).first()
    if row is None:
        raise HTTPException(status_code=404, detail="别名不存在")
    db.delete(row)
    db.commit()
    return {"deleted": alias}


# ---------------------------------------------------------------- 人工复核闭环
@router.get("/api/review/queue")
def api_review_queue(
    chat_id: str | None = None,
    limit: int = 100,
    low_conf: float = 0.5,
    db: Session = Depends(get_session),
) -> dict:
    """待人工复核队列:未分类组 + 低置信组(未分类优先),附样本文本。

    这是「未分类静默堆积」唯一的出口——队列长度不降,说明要么闭集缺类,
    要么提示词/召回还不到位。
    """
    from app.services.topic_review import list_review_queue

    return list_review_queue(db, chat_id=chat_id, limit=limit, low_conf=low_conf)


class ReviewApplyIn(BaseModel):
    """人工定标入参:把某组确认到某个标准标签。"""

    chat_id: str
    root_message_id: str
    label: str
    allow_new: bool = False
    propagate: bool = False


@router.post("/api/review/apply")
def api_review_apply(
    body: ReviewApplyIn,
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
) -> dict:
    """人工确认一组的标签:回写库(置 source=human,conf=1.0)+ 反哺别名表。

    标签收敛不到标准集时返回 400 且不改库(人工也可能手滑写错字)。
    """
    from app.services.topic_review import apply_review

    result = apply_review(
        db,
        body.chat_id,
        body.root_message_id,
        body.label,
        allow_new=body.allow_new,
        propagate=body.propagate,
    )
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["reason"])
    return result


@router.get("/api/review/gold")
def api_review_gold(
    chat_id: str | None = None, db: Session = Depends(get_session)
) -> dict:
    """gold set 规模与标签分布(人工确认过的组)。"""
    from app.services.topic_review import gold_stats

    return gold_stats(db, chat_id=chat_id)


# ---------------------------------------------------------------- 指标埋点(阶段四 P2)
@router.get("/api/topic_metrics")
def api_topic_metrics(db: Session = Depends(get_session)) -> dict:
    """话题归纳量化指标(只读,不改库),供度量「标签治理 / 归并 / 缝合」效果。

    - topic_count / low_frequency_topic_count : 标签总数,以及仅出现 1 个组的
      低频标签数与占比(同义词漂移的直接度量,目标:占比下降)
    - normalization: 归一化收敛率 = (别名命中 + 相似度兜底) / 归一化总数,
      衡量别名表覆盖率(进程内累计近似,见 label_norm.get_norm_stats)
    - merge_rate: 收敛率 = 已收敛到单组的 (chat,topic) 占比;
      topics_with_multiple_groups 为仍待归并的碎片主题数
    - stitching: 缝合确认率 = 确定性 case_key 命中 / (case_key + LLM) 判定总数,
      衡量确定性锚点接管比例(进程内累计近似,见 conv_stitch.get_stitch_counts)
    - false_merge_sample: 误合率需人工复核,此处返回抽样状态占位(不自动判定)
    - unclassified: 未分类组数/占比(闭集判别上线后最需要盯的指标:非标准标签被
      拒会静默落成未分类,不盯就会越堆越多)
    - standard_coverage: 标准标签被用到的比例(闭集覆盖不到的现象 = 长期未分类)
    - confidence: 已归类组的置信度分布(取组内最大 topic_conf),供低置信复核排期
    - recall: 候选召回命中情况(进程内累计)——narrow_rate 是「被判窄化」的调用占比,
      fallback_rate 是「窄化后仍判 UNKNOWN 用全集重问」的占比,**这个值就是召回的
      漏检率**,持续偏高说明召回打分需要调(top_k 调大或换样例)
    - review: 人工复核闭环——pending 是待复核组数(未分类 + 低置信),gold_groups 是
      人工确认过的组数。pending 不降说明缺类/提示词不到位;**gold_groups=0 时上面
      所有准确率指标都只是模型自说自话**,没有任何基准可比
    """
    from sqlalchemy import case, func

    from app.services.conv_stitch import get_stitch_counts
    from app.services.label_norm import get_norm_stats
    from app.services.topic_recall import get_stats as get_recall_stats
    from app.services.topic_review import gold_stats, list_review_queue
    from app.services.topic_service import get_standard_labels

    # ---- 标签分布:每 (chat_id, topic) 的分组数(组=distinct root_message_id) ----
    #     每个 (chat, topic) 的理想终态是 1 组(碎片已并回原问题块/缝合已关联)。
    ct_groups = {
        (c, t): n
        for c, t, n in db.query(
            Message.chat_id,
            Message.topic,
            func.count(func.distinct(Message.root_message_id)),
        )
        .where(Message.topic != "", Message.root_message_id != "")
        .group_by(Message.chat_id, Message.topic)
        .all()
    }
    topic_count = db.query(func.count(func.distinct(Message.topic))).where(
        Message.topic != ""
    ).scalar() or 0

    # 低频标签:该 topic 在任意群均仅 1 组
    topic_group_total = {t: 0 for t in {t for (_c, t) in ct_groups}}
    for (_c, t), n in ct_groups.items():
        topic_group_total[t] += n
    low_freq = sum(1 for n in topic_group_total.values() if n <= 1)

    # ---- 归一化收敛率(进程内累计) ----
    ns = get_norm_stats()
    converged = ns["alias_hit"] + ns["similarity_hit"]
    normalization_rate = round(converged / ns["total"], 4) if ns["total"] else 0.0

    # ---- 归并率:已收敛到单组的 (chat, topic) 占比 ----
    merged_topics = sum(1 for n in ct_groups.values() if n == 1)
    ct_total = len(ct_groups)
    merge_rate = round(merged_topics / ct_total, 4) if ct_total else 0.0

    # ---- 缝合确认率(进程内累计) ----
    ck, llm_n = get_stitch_counts()
    stitch_total = ck + llm_n
    stitch_confirm_rate = round(ck / stitch_total, 4) if stitch_total else 0.0

    # ---- 未分类率 + 置信度分布(组维度:一个组只要有非空 topic 即算已归类) ----
    group_rows = (
        db.query(
            Message.chat_id,
            Message.root_message_id,
            func.max(case((Message.topic != "", 1), else_=0)),
            func.max(Message.topic_conf),
        )
        .where(Message.root_message_id != "")
        .group_by(Message.chat_id, Message.root_message_id)
        .all()
    )
    total_groups = len(group_rows)
    labeled_groups = sum(1 for r in group_rows if r[2])
    unclassified_groups = total_groups - labeled_groups
    # 置信度分档:高(≥0.8)自动 / 中(0.5~0.8)待复核 / 低(0~0.5)转人工 / 无(0=旧格式)
    conf_buckets = {"high": 0, "medium": 0, "low": 0, "none": 0}
    for _c, _r, labeled, conf in group_rows:
        if not labeled:
            continue
        c = float(conf or 0.0)
        if c <= 0:
            conf_buckets["none"] += 1
        elif c >= 0.8:
            conf_buckets["high"] += 1
        elif c >= 0.5:
            conf_buckets["medium"] += 1
        else:
            conf_buckets["low"] += 1

    # ---- 标准标签覆盖率 ----
    standards = set(get_standard_labels(db))
    used_labels = {t for (_c, t) in ct_groups}
    covered = len(standards & used_labels)

    # ---- 人工复核闭环(review_queue 只取 1 条探针,total 是聚合计数) ----
    queue = list_review_queue(db, limit=1)
    gold = gold_stats(db)

    return {
        "topic_count": int(topic_count),
        "low_frequency_topic_count": int(low_freq),
        "low_frequency_ratio": round(low_freq / topic_count, 4) if topic_count else 0.0,
        "normalization": {
            "converged": int(converged),
            "total": int(ns["total"]),
            "alias_hit": int(ns["alias_hit"]),
            "similarity_hit": int(ns["similarity_hit"]),
            "standard_hit": int(ns["standard_hit"]),
            "rate": normalization_rate,
        },
        "merge_rate": merge_rate,
        "remaining_fragmented_topics": sum(1 for n in ct_groups.values() if n > 1),
        "stitch_confirm_rate": stitch_confirm_rate,
        "stitching": {"case_key": ck, "llm": llm_n},
        "unclassified": {
            "groups": int(unclassified_groups),
            "total_groups": int(total_groups),
            "rate": (
                round(unclassified_groups / total_groups, 4) if total_groups else 0.0
            ),
        },
        "standard_coverage": {
            "standard_count": len(standards),
            "used_standard_count": covered,
            "rate": round(covered / len(standards), 4) if standards else 0.0,
            "unused_standards": sorted(standards - used_labels)[:20],
        },
        "confidence": conf_buckets,
        "recall": get_recall_stats(),
        "review": {
            "pending_groups": queue["total"],
            "pending_unclassified": unclassified_groups,
            "low_conf_threshold": 0.5,
            "gold_groups": gold["gold_groups"],
            "human_rate": gold["human_rate"],
            "gold_labels_top": dict(list(gold["labels"].items())[:10]),
        },
        "false_merge_sample": {
            "status": "needs_manual_review",
            "sample_size": 0,
            "rate": None,
        },
    }


# ---------------------------------------------------------------- 案例软合并(阶段三)
@router.get("/api/cases/stats")
def api_cases_stats(db: Session = Depends(get_session)) -> dict:
    """软合并指标(阶段三最小量化集),只读不写库。

    - case_groups / linked_conversations / unclassified: 软合并组数/涉及会话数/独立会话数
    - case_key_stitches / llm_stitches: 确定性锚点 vs LLM 缝合计数(日志累计,进程内近似)
    """
    from sqlalchemy import func

    from app.services.conv_stitch import get_stitch_counts

    linked = (
        db.query(func.count(Conversation.id))
        .where(Conversation.case_id != 0)
        .scalar()
        or 0
    )
    case_groups = (
        db.query(func.count(func.distinct(Conversation.case_id)))
        .where(Conversation.case_id != 0)
        .scalar()
        or 0
    )
    unclassified = (
        db.query(func.count(Conversation.id))
        .where(Conversation.case_id == 0)
        .scalar()
        or 0
    )
    ck, llm_n = get_stitch_counts()
    return {
        "case_groups": int(case_groups),
        "linked_conversations": int(linked),
        "independent_conversations": int(unclassified),
        "case_key_stitches": ck,
        "llm_stitches": llm_n,
    }


@router.get("/api/cases")
def api_cases_list(db: Session = Depends(get_session)) -> dict:
    """列出全部软合并案例组(case_id>0 去重),附成员概览,供「待确认」列表。"""
    from sqlalchemy import func

    rows = (
        db.query(
            Conversation.chat_id,
            Conversation.case_id,
            func.count(Conversation.id).label("cnt"),
            func.min(Conversation.id).label("rep_id"),
            func.max(Conversation.last_message_at_ms).label("last_ms"),
        )
        .where(Conversation.case_id != 0)
        .group_by(Conversation.chat_id, Conversation.case_id)
        .order_by(Conversation.chat_id, func.min(Conversation.id))
        .all()
    )
    groups = []
    for chat_id, case_id, cnt, rep_id, last_ms in rows:
        rep = db.get(Conversation, rep_id)
        groups.append(
            {
                "chat_id": chat_id,
                "case_id": int(case_id),
                "member_count": int(cnt),
                "topic": rep.topic if rep else "",
                "root_message_id": rep.root_message_id if rep else "",
                "last_message_at_ms": int(last_ms or 0),
            }
        )
    return {"cases": groups}


@router.get("/api/cases/{case_id}/timeline")
def api_case_timeline(case_id: int, db: Session = Depends(get_session)) -> dict:
    """某案例组的完整时间线:跨会话、按时间平铺的消息(供前端聚合渲染)。"""
    from app.dao.conversation_repo import ConversationRepo
    from app.services.user_name_service import fill_tree_sender_names

    convs = (
        db.query(Conversation)
        .where(Conversation.case_id == case_id)
        .order_by(Conversation.id)
        .all()
    )
    if not convs:
        raise HTTPException(status_code=404, detail="案例组不存在")
    chat_id = convs[0].chat_id

    flat = []
    for c in convs:
        tree = ConversationRepo(db).list_tree(c.id)
        for node in tree["roots"]:
            flat.extend(_walk_tree(node))
    # 去重(同一消息可能在多节点父链上),按时间/消息序排
    seen: set[str] = set()
    out = []
    for m in flat:
        if m["message_id"] in seen:
            continue
        seen.add(m["message_id"])
        out.append(m)
    fill_tree_sender_names(out)
    out.sort(key=lambda m: (int(m["create_time"]) if m["create_time"].isdigit() else 0, m["message_id"]))
    return {"chat_id": chat_id, "case_id": case_id, "messages": out}


def _walk_tree(node: dict):
    """把 list_tree 的嵌套节点平铺为 [dict, ...](深度优先)。"""
    out = [node["message"]]
    for ch in node.get("children") or []:
        out.extend(_walk_tree(ch))
    return out


@router.post("/api/cases/{case_id}/merge")
def api_case_merge(
    case_id: int,
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
) -> dict:
    """管理员确认:把案例组物理合并为一条对话(并入最早会话为根)。"""
    from app.services.case_merge import merge_case

    conv = db.query(Conversation).filter(Conversation.case_id == case_id).first()
    if conv is None:
        raise HTTPException(status_code=404, detail="案例组不存在")
    merged = merge_case(db, conv.chat_id, case_id)
    return {"case_id": case_id, "merged": merged}


@router.post("/api/cases/{case_id}/unlink")
def api_case_unlink(
    case_id: int,
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
) -> dict:
    """解绑案例组:共享该 case_id 的会话全部归 0(可回滚,数据零损失)。"""
    from app.services.case_merge import unlink_case

    conv = db.query(Conversation).filter(Conversation.case_id == case_id).first()
    if conv is None:
        raise HTTPException(status_code=404, detail="案例组不存在")
    n = unlink_case(db, conv.chat_id, case_id, commit=True)
    return {"case_id": case_id, "unlinked": n}


class TopicKnowledgeIn(BaseModel):
    """主题参考知识库入参:同名 upsert,整体覆盖 content。"""

    topic: str
    content: str


class TopicAliasIn(BaseModel):
    """标签别名入参:把 alias 收敛到 canonical。"""

    alias: str
    canonical: str


@router.get("/api/topic_knowledge")
def api_topic_knowledge_get(topic: str, db: Session = Depends(get_session)) -> dict:
    from app.models.topic_knowledge import TopicKnowledge

    row = db.query(TopicKnowledge).filter(TopicKnowledge.topic == topic).first()
    return {"topic": topic, "content": row.content if row else ""}


@router.post("/api/topic_knowledge")
def api_topic_knowledge_upsert(
    body: TopicKnowledgeIn,
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
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
def api_topic_knowledge_delete(
    topic: str,
    db: Session = Depends(get_session),
    user: User = Depends(require_admin),
) -> dict:
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
        # 处理阶段(与主题正交):待处理/处理中/已推送/已上门/已解决;空=未判定
        "stage": conv.stage or "",
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
