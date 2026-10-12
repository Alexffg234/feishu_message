"""LLM 主题分类服务。

- 入库不阻塞:消息落库后由后台线程池异步分类
- 以「回复链分组」为粒度打标签(1 组 = 1 次 LLM 调用),组内共享同一 topic
- 分类时把库里已有主题列表传给 LLM,保证同义话题沿用同一标签
- 未配置 LLM / 调用失败时优雅降级:topic 留空,后续可重分类
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import lru_cache

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.dao.message_repo import MessageRepo
from app.services.llm import LLMClient, LLMError

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "你是群聊消息主题分类器。请把下面这段对话归纳为一个简短主题标签。\n"
    "要求:\n"
    "1. 标签为 2~10 字中文短语,概括对话讨论的功能或问题(例如:投屏、远场语音唤不醒、蓝牙连接)。\n"
    "2. 若「已有主题」列表中存在相近主题,必须原样沿用该标签,不得新造同义标签。\n"
    "3. 只输出标签本身,不要引号、标点或任何解释。"
)

# 分类失败后,同一组的最小重试间隔(秒),防止 LLM 故障时反复打
_RETRY_COOLDOWN = 60
# 送给 LLM 的每组消息条数上限(控制 token)
_MAX_ROWS_PROMPT = 20


def _fmt_time(create_time: str) -> str:
    """毫秒时间戳字符串 -> 可读时间;失败原样返回。"""
    try:
        ts = int(create_time) / 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return create_time or "-"


def _clean_label(raw: str) -> str:
    """清洗 LLM 输出:去引号/空白/首尾标点,截断到 20 字。"""
    label = (raw or "").strip().strip("\"'“”‘’「」 ")
    label = label.strip("。.!,!;；:：")
    if len(label) > 20:
        label = label[:20]
    return label


class TopicService:
    def __init__(
        self,
        session_factory=SessionLocal,
        llm: LLMClient | None = None,
    ):
        self._session_factory = session_factory
        self._llm = llm
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="topic")
        self._lock = threading.Lock()
        self._pending: set[tuple[str, str]] = set()
        self._last_failure: dict[tuple[str, str], float] = {}

    @property
    def enabled(self) -> bool:
        return self._llm is not None

    # ---------------------------------------------------------------- 分类
    def schedule_group(self, chat_id: str, root_message_id: str) -> bool:
        """为某回复链组调度异步主题分类。

        带去重(同一组已在队列中则跳过)与失败冷却。返回是否已调度。
        """
        if not self.enabled:
            return False
        key = (chat_id, root_message_id)
        now = time.monotonic()
        with self._lock:
            if key in self._pending:
                return False
            failed_at = self._last_failure.get(key)
            if failed_at is not None and now - failed_at < _RETRY_COOLDOWN:
                return False
            self._pending.add(key)
        self._executor.submit(self._classify_worker, chat_id, root_message_id)
        return True

    def _classify_worker(self, chat_id: str, root_message_id: str) -> None:
        key = (chat_id, root_message_id)
        try:
            self.classify_group(chat_id, root_message_id)
        except Exception:  # noqa: BLE001
            logger.exception("主题分类失败 %s/%s", chat_id, root_message_id)
            with self._lock:
                self._last_failure[key] = time.monotonic()
        finally:
            with self._lock:
                self._pending.discard(key)

    def classify_group(self, chat_id: str, root_message_id: str) -> str:
        """同步为一组消息打主题标签(亦供 --reclassify 使用)。

        已分类的组直接返回现有标签。失败抛 LLMError。
        """
        if not self.enabled:
            raise LLMError("LLM 未配置,无法分类")
        with self._session_factory() as session:
            repo = MessageRepo(session)
            rows = repo.list_by_root(root_message_id, chat_id)
            if not rows:
                return ""
            existing_topic = next((m.topic for m in rows if m.topic), "")
            if existing_topic:
                return existing_topic
            existing = repo.get_existing_topics(limit=50)
            label = self._llm_label(rows, existing)
            if not label:
                raise LLMError("LLM 返回空标签")
            repo.set_topic_for_group(chat_id, root_message_id, label)
            session.commit()
            logger.info("主题分类 %s/%s -> %s", chat_id, root_message_id, label)
            return label

    def wait_idle(self, timeout: float = 30.0) -> None:
        """等待后台分类任务全部完成(测试用)。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if not self._pending:
                    return
            time.sleep(0.05)

    # ---------------------------------------------------------------- 查询
    def list_topics(
        self, chat_id: str | None = None, limit: int = 50
    ) -> list[dict]:
        """按主题聚合最近活跃的话题,主题下嵌套回复链分组。

        每组: {topic, group_count, message_count, senders, groups:[MessageGroupOut 结构]}
        """
        with self._session_factory() as session:
            repo = MessageRepo(session)
            topics = repo.list_topics(chat_id=chat_id, limit=limit)
            result: list[dict] = []
            for topic, _last in topics:
                rows = repo.list_by_topic(topic, chat_id=chat_id)
                groups: dict[tuple[str, str], list] = {}
                for m in rows:
                    groups.setdefault((m.chat_id, m.root_message_id), []).append(m)

                group_outs: list[dict] = []
                senders: list[str] = []
                for (cid, root_id), grows in groups.items():
                    root_row = next((m for m in grows if m.message_id == root_id), None)
                    replies = [m for m in grows if m.message_id != root_id]
                    gsenders: list[str] = []
                    for m in grows:
                        if m.sender_id and m.sender_id not in gsenders:
                            gsenders.append(m.sender_id)
                        if m.sender_id and m.sender_id not in senders:
                            senders.append(m.sender_id)
                    group_outs.append(
                        {
                            "root_message_id": root_id,
                            "chat_id": cid,
                            "root": root_row,
                            "replies": replies,
                            "message_count": len(grows),
                            "senders": gsenders,
                        }
                    )

                def _latest(g: dict) -> int:
                    ids = [m.id for m in g["replies"]]
                    if g["root"] is not None:
                        ids.append(g["root"].id)
                    return max(ids) if ids else 0

                group_outs.sort(key=_latest, reverse=True)
                result.append(
                    {
                        "topic": topic,
                        "group_count": len(group_outs),
                        "message_count": len(rows),
                        "senders": senders,
                        "groups": group_outs,
                    }
                )
            return result

    # ---------------------------------------------------------------- 内部
    def _llm_label(self, rows: list, existing_topics: list[str]) -> str:
        lines = []
        for m in rows[:_MAX_ROWS_PROMPT]:
            text = (m.text or "").strip() or f"({m.message_type or '非文本'})"
            lines.append(f"[{_fmt_time(m.create_time)}] {m.sender_id or '?'}: {text}")
        existing_str = ", ".join(existing_topics) if existing_topics else "(暂无)"
        user_prompt = (
            f"已有主题: {existing_str}\n\n"
            f"对话内容:\n" + "\n".join(lines)
        )
        raw = self._llm.chat(_SYSTEM_PROMPT, user_prompt)
        return _clean_label(raw)


@lru_cache
def get_topic_service() -> TopicService:
    """应用级单例。未配置 LLM 时返回 disabled 实例(查询仍可用)。"""
    settings = get_settings()
    llm: LLMClient | None = None
    if settings.llm_enabled:
        llm = LLMClient(
            settings.llm_api_base,
            settings.llm_api_key,
            settings.llm_model,
            settings.llm_timeout,
        )
    else:
        logger.info("LLM 主题分类未配置(LLM_API_KEY/LLM_MODEL),已禁用")
    return TopicService(llm=llm)
