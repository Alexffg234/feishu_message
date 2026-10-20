"""主题 AI 总结服务(后台定时全量预生成)。

- 对每个 (chat_id, topic) 让 LLM 阅读该主题下全部对话上下文,生成 4 块分析:
  problem(问题是什么)/ investigation(如何排查)/ resolution(如何解决)/ status(当前状态)
- 后台守护线程按 topic_summary_interval_minutes 周期全量跑一轮:
  抢 DB 租约(多 worker 下同轮只有一个执行)→ 枚举全部 topic → 比对指纹
  → 仅对缺失/过期的串行生成落库(串行防 LLM 限流)
- 页面 API 只读缓存,秒回;手动 POST /api/topics/summary 可强制重生成
- LLM 未配置时优雅降级:调度器 no-op,API 返回 enabled=false
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.app_lock import SchedulerLock
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.topic_summary import TopicSummary
from app.services.llm import LLMClient, LLMError

logger = logging.getLogger(__name__)

# 租约锁名称与时长(单轮 15 个 topic 约 2~5 分钟,60 分钟足够)
_LOCK_NAME = "topic_summary_round"
_LOCK_LEASE_MINUTES = 60

# 上下文规模控制(防 token 爆炸)
_MAX_TEXT_LINES = 40  # 最多送给 LLM 的文本消息条数
_MAX_TEXT_LEN = 200  # 每条文本截断长度
_MIN_TEXT_LINES = 3  # 有效文本少于该数的 topic 不生成(纯图片/语音无意义)

_SYSTEM_PROMPT = (
    "你是 TV 产品故障排查专家。下面是一个飞书技术支持群里围绕同一问题类别的多段对话"
    "(可能涉及多台机器、多位用户,按时间顺序排列,「案例 N」表示一次独立的反馈/排查过程)。\n"
    "请基于上下文,输出严格的 JSON(不要 markdown 代码块、不要 JSON 之外的任何文字):\n"
    "{\n"
    '  "problem": "问题是什么:1~3 句话概括该类别问题的现象、范围与影响",\n'
    '  "investigation": "如何排查:给出具体、可执行的排查步骤,分点列出",\n'
    '  "resolution": "如何解决:给出解决方案或当前采用的处理办法,分点列出;若尚未解决,写当前进展与下一步计划",\n'
    '  "status": "从[已解决, 排查中, 待用户反馈, 未知]中选一个"\n'
    "}"
)

_STATUS_ENUM = {"已解决", "排查中", "待用户反馈", "未知"}


def _fmt_ts(create_time: str) -> str:
    """毫秒时间戳字符串 -> 'MM-DD HH:MM';失败原样返回。"""
    try:
        ts = int(create_time) / 1000
        return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return create_time or "-"


def _parse_llm_json(raw: str) -> dict | None:
    """从 LLM 输出中提取 JSON 对象;失败返回 None。"""
    text = (raw or "").strip()
    if text.startswith("```"):  # 去 markdown 代码块围栏
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _media_label(m: Message) -> str:
    """非文本消息的占位描述(让 LLM 知道这里有过图片/视频等证据)。"""
    mtype = m.message_type or ""
    if mtype == "image":
        return "(图片)"
    if mtype == "media":
        return "(视频)"
    if mtype == "file":
        return f"(文件: {m.file_name or '未知文件'})"
    if mtype == "audio":
        return "(语音)"
    return f"({mtype or '非文本'})"


class TopicSummaryService:
    def __init__(
        self,
        session_factory=SessionLocal,
        llm: LLMClient | None = None,
        interval_seconds: int | None = None,
        startup_delay_seconds: float = 15.0,
    ):
        self._session_factory = session_factory
        self._llm = llm
        settings = get_settings()
        if interval_seconds is None:
            interval_seconds = settings.topic_summary_interval_minutes * 60
        self._interval = max(interval_seconds, 5)
        self._startup_delay = startup_delay_seconds
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._round_lock = threading.Lock()  # 进程内:同一 worker 不重叠跑轮次

    @property
    def enabled(self) -> bool:
        return self._llm is not None

    # ============================================================ 单主题生成
    def _fingerprint(self, session, chat_id: str, topic: str) -> tuple[int, int] | None:
        from sqlalchemy import func

        row = session.execute(
            select(
                func.count(Message.id),
                func.max(Message.id),
            ).where(Message.chat_id == chat_id, Message.topic == topic)
        ).one()
        count, max_id = int(row[0] or 0), row[1]
        if count == 0:
            return None
        return count, int(max_id or 0)

    def _row_to_dict(self, row: TopicSummary, stale: bool = False) -> dict:
        return {
            "chat_id": row.chat_id,
            "topic": row.topic,
            "problem": row.problem,
            "investigation": row.investigation,
            "resolution": row.resolution,
            "status": row.status,
            "message_count": row.message_count,
            "max_message_id": row.max_message_id,
            "generated_at": row.generated_at.isoformat() if row.generated_at else "",
            "stale": stale,
        }

    def get_cached(self, chat_id: str, topic: str) -> dict:
        """纯读缓存(页面 API 用,不调 LLM)。无缓存返回 found=False。"""
        with self._session_factory() as session:
            row = session.execute(
                select(TopicSummary).where(
                    TopicSummary.chat_id == chat_id,
                    TopicSummary.topic == topic,
                )
            ).scalar_one_or_none()
            if row is None:
                return {"chat_id": chat_id, "topic": topic, "found": False,
                        "enabled": self.enabled}
            fp = self._fingerprint(session, chat_id, topic)
            stale = bool(fp and (row.message_count, row.max_message_id) != fp)
            out = self._row_to_dict(row, stale=stale)
            out["found"] = True
            out["enabled"] = self.enabled
            return out

    def _build_context(self, session, chat_id: str, topic: str) -> str | None:
        """把该 topic 下所有对话拼成 LLM 上下文;有效文本不足返回 None。"""
        convs = (
            session.execute(
                select(Conversation)
                .where(Conversation.chat_id == chat_id, Conversation.topic == topic)
                .order_by(Conversation.started_at_ms.asc(), Conversation.id.asc())
            )
            .scalars()
            .all()
        )
        # 兜底:有消息但对话记录缺失(未回填)时直接按消息聚合
        if not convs:
            convs = [None]  # type: ignore[list-item]

        sender_ids: set[str] = set()
        case_msgs: list[tuple[Conversation | None, list[Message]]] = []
        for conv in convs:
            if conv is not None:
                msgs = (
                    session.execute(
                        select(Message)
                        .where(Message.conversation_id == conv.id)
                        .order_by(Message.received_at.asc(), Message.id.asc())
                    )
                    .scalars()
                    .all()
                )
            else:
                msgs = (
                    session.execute(
                        select(Message)
                        .where(Message.chat_id == chat_id, Message.topic == topic)
                        .order_by(Message.received_at.asc(), Message.id.asc())
                    )
                    .scalars()
                    .all()
                )
            if not msgs:
                continue
            for m in msgs:
                if m.sender_id:
                    sender_ids.add(m.sender_id)
            case_msgs.append((conv, list(msgs)))
        if not case_msgs:
            return None

        from app.services.user_name_service import get_user_name_service

        name_map = get_user_name_service().resolve_many(sorted(sender_ids))

        lines: list[str] = []
        text_lines = 0
        case_no = 0
        for conv, msgs in case_msgs:
            first = _fmt_ts(msgs[0].create_time)
            last = _fmt_ts(msgs[-1].create_time)
            case_no += 1
            lines.append(f"--- 案例 {case_no}({first} ~ {last})---")
            for m in msgs:
                if text_lines >= _MAX_TEXT_LINES:
                    lines.append("(后续消息略)")
                    break
                text = (m.text or "").strip()
                if text:
                    name = name_map.get(m.sender_id) or m.sender_id or "?"
                    lines.append(f"[{_fmt_ts(m.create_time)}] {name}: {text[:_MAX_TEXT_LEN]}")
                    text_lines += 1
                else:
                    lines.append(f"[{_fmt_ts(m.create_time)}] ({m.sender_id or '?'}): "
                                 f"{_media_label(m)}")
            if text_lines >= _MAX_TEXT_LINES:
                break
        if text_lines < _MIN_TEXT_LINES:
            return None
        return "\n".join(lines)

    def generate_topic(self, chat_id: str, topic: str, force: bool = False) -> dict | None:
        """为某主题生成(或取缓存)AI 分析。

        - 指纹未变且非 force:直接返回缓存,不调 LLM
        - 有效文本不足:返回 None(该 topic 不适合总结)
        返回 dict(见 _row_to_dict,另含 generated_now)。
        """
        if not self.enabled:
            return None
        with self._session_factory() as session:
            fp = self._fingerprint(session, chat_id, topic)
            if fp is None:
                return None
            row = session.execute(
                select(TopicSummary).where(
                    TopicSummary.chat_id == chat_id,
                    TopicSummary.topic == topic,
                )
            ).scalar_one_or_none()
            if row is not None and not force and (row.message_count, row.max_message_id) == fp:
                out = self._row_to_dict(row)
                out["generated_now"] = False
                return out

            context = self._build_context(session, chat_id, topic)
            if context is None:
                return None

        try:
            raw = self._llm.chat(_SYSTEM_PROMPT, context)
        except LLMError as e:
            logger.warning("主题总结生成失败 %s/%s: %s", chat_id, topic, e)
            return None

        data = _parse_llm_json(raw)
        if data is None:
            # 降级:LLM 没按 JSON 输出,原文塞进 problem
            data = {"problem": raw, "investigation": "", "resolution": "", "status": "未知"}
        status = str(data.get("status") or "未知").strip()
        if status not in _STATUS_ENUM:
            status = "未知"

        with self._session_factory() as session:
            row = session.execute(
                select(TopicSummary).where(
                    TopicSummary.chat_id == chat_id,
                    TopicSummary.topic == topic,
                )
            ).scalar_one_or_none()
            if row is None:
                row = TopicSummary(chat_id=chat_id, topic=topic)
                session.add(row)
            row.problem = str(data.get("problem") or "").strip()
            row.investigation = str(data.get("investigation") or "").strip()
            row.resolution = str(data.get("resolution") or "").strip()
            row.status = status
            row.message_count, row.max_message_id = fp
            row.generated_at = datetime.now(timezone.utc)
            session.commit()
            out = self._row_to_dict(row)
            out["generated_now"] = True
            logger.info("主题总结生成 %s/%s status=%s", chat_id, topic, status)
            return out

    # ============================================================ 全量轮次
    def _acquire_lock(self, token: str) -> bool:
        now = datetime.now(timezone.utc)
        with self._session_factory() as session:
            row = session.get(SchedulerLock, _LOCK_NAME)
            if row is None:
                session.add(SchedulerLock(name=_LOCK_NAME, token=token,
                                          expires_at=now + timedelta(minutes=_LOCK_LEASE_MINUTES)))
                session.commit()
                return True
            exp = row.expires_at
            if exp.tzinfo is None:  # SQLite 不存时区,裸时间按 UTC 解释
                exp = exp.replace(tzinfo=timezone.utc)
            if exp <= now or row.token == token:
                row.token = token
                row.expires_at = now + timedelta(minutes=_LOCK_LEASE_MINUTES)
                session.commit()
                return True
            return False

    def _release_lock(self, token: str) -> None:
        from sqlalchemy import delete

        try:
            with self._session_factory() as session:
                session.execute(
                    delete(SchedulerLock).where(
                        SchedulerLock.name == _LOCK_NAME,
                        SchedulerLock.token == token,
                    )
                )
                session.commit()
        except Exception:  # noqa: BLE001
            logger.exception("释放调度租约失败(租约到期会自动过期)")

    def _list_topics(self) -> list[tuple[str, str]]:
        with self._session_factory() as session:
            rows = session.execute(
                select(Message.chat_id, Message.topic)
                .where(Message.topic != "")
                .distinct()
                .order_by(Message.topic)
            ).all()
        return [(r[0], r[1]) for r in rows]

    def run_round(self) -> int:
        """全量跑一轮:对所有 topic 检查指纹,缺失/过期的重新生成。

        返回本轮实际生成的数量;抢不到租约(其他 worker 在跑)返回 0。
        """
        if not self.enabled:
            return 0
        if not self._round_lock.acquire(blocking=False):
            logger.debug("上一轮主题总结尚未结束,跳过本轮")
            return 0
        token = uuid.uuid4().hex
        try:
            if not self._acquire_lock(token):
                logger.debug("主题总结租约被其他 worker 持有,跳过本轮")
                return 0
            try:
                generated = 0
                for chat_id, topic in self._list_topics():
                    try:
                        out = self.generate_topic(chat_id, topic)
                        if out and out.get("generated_now"):
                            generated += 1
                    except Exception:  # noqa: BLE001
                        logger.exception("主题总结生成异常 %s/%s", chat_id, topic)
                logger.info("主题总结轮次完成: 共 %d 个 topic,生成 %d 个",
                            len(self._list_topics()), generated)
                return generated
            finally:
                self._release_lock(token)
        finally:
            self._round_lock.release()

    # ============================================================ 后台调度
    def start(self) -> None:
        """启动后台守护线程(幂等)。LLM 未配置或间隔<=0 时 no-op。"""
        if not self.enabled:
            logger.info("主题 AI 总结未启用(LLM 未配置),后台调度不启动")
            return
        if get_settings().topic_summary_interval_minutes <= 0:
            logger.info("主题 AI 总结后台调度已禁用(topic_summary_interval_minutes=0)")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="topic-summary"
        )
        self._thread.start()
        logger.info("主题 AI 总结后台调度已启动(每 %d 分钟一轮)",
                    get_settings().topic_summary_interval_minutes)

    def _loop(self) -> None:
        if self._startup_delay > 0:
            self._stop_evt.wait(self._startup_delay)
        while not self._stop_evt.is_set():
            try:
                self.run_round()
            except Exception:  # noqa: BLE001
                logger.exception("主题总结轮次异常")
            self._stop_evt.wait(self._interval)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None


@lru_cache
def get_topic_summary_service() -> TopicSummaryService:
    """应用级单例。未配置 LLM 时返回 disabled 实例(查询接口仍可用)。"""
    settings = get_settings()
    llm: LLMClient | None = None
    if settings.llm_enabled:
        llm = LLMClient(
            settings.llm_api_base,
            settings.llm_api_key,
            settings.llm_model,
            settings.llm_timeout,
        )
    return TopicSummaryService(llm=llm)
