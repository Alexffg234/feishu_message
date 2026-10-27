"""会话缝合后台调度:周期性地让 LLM 判定同群相邻会话是否同一问题处理生命周期。

与同标签归并(merge_topic_group)互补:标准标签「只归类不合并」(不同案例共享标准名),
同一案例的续聊靠本服务自动缝合,无需人工跑脚本。

- 后台守护线程按 conv_stitch_interval_minutes 周期跑一轮:
  抢 DB 租约(多 worker 下同轮只有一个执行)→ 枚举全部群 →
  逐群跑 stitch_chat(LLM 判定 + 护栏;同一案例续聊不画横杠)
- 水位线:每群 (会话数, 最大消息 id) 不变则跳过,省 LLM 调用;
  有新消息(可能是续聊碎片)才重新判定。水位线存内存,进程重启后多跑一轮,幂等无害。
- LLM 未配置时优雅降级:调度器 no-op。
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.app_lock import SchedulerLock
from app.models.conversation import Conversation
from app.models.message import Message
from app.services.conv_stitch import stitch_chat
from app.services.llm import LLMClient

logger = logging.getLogger(__name__)

# 租约锁名称与时长(单个大群一轮全量判定约 10~20 分钟,60 分钟足够)
_LOCK_NAME = "conv_stitch_round"
_LOCK_LEASE_MINUTES = 60


class ConvStitchService:
    def __init__(
        self,
        session_factory=SessionLocal,
        llm: LLMClient | None = None,
        interval_seconds: int | None = None,
        startup_delay_seconds: float = 30.0,
    ):
        self._session_factory = session_factory
        self._llm = llm
        settings = get_settings()
        if interval_seconds is None:
            interval_seconds = settings.conv_stitch_interval_minutes * 60
        self._interval = max(interval_seconds, 5)
        self._startup_delay = startup_delay_seconds
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._round_lock = threading.Lock()  # 进程内:同一 worker 不重叠跑轮次
        self._watermarks: dict[str, tuple[int, int]] = {}  # chat -> (会话数, 最大消息id)

    @property
    def enabled(self) -> bool:
        return self._llm is not None

    # ============================================================ 单轮
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

    def _chat_watermark(self, session, chat_id: str) -> tuple[int, int] | None:
        """群水位线 = (会话数, 最大消息 id);无消息返回 None。"""
        conv_n = (
            session.query(func.count(Conversation.id))
            .filter(Conversation.chat_id == chat_id)
            .scalar() or 0
        )
        max_msg_id = (
            session.query(func.max(Message.id))
            .filter(Message.chat_id == chat_id)
            .scalar()
        )
        if not max_msg_id:
            return None
        return int(conv_n), int(max_msg_id)

    def _list_chats(self) -> list[str]:
        with self._session_factory() as session:
            rows = session.execute(
                select(Conversation.chat_id).distinct().order_by(Conversation.chat_id)
            ).all()
        return [r[0] for r in rows]

    def run_round(self) -> int:
        """跑一轮:对所有水位线变化的群执行会话缝合。

        返回本轮实际合并的 (源,目标) 对数;抢不到租约(其他 worker 在跑)返回 0。
        """
        if not self.enabled:
            return 0
        if not self._round_lock.acquire(blocking=False):
            logger.debug("上一轮会话缝合尚未结束,跳过本轮")
            return 0
        token = uuid.uuid4().hex
        try:
            if not self._acquire_lock(token):
                logger.debug("会话缝合租约被其他 worker 持有,跳过本轮")
                return 0
            try:
                total = 0
                for chat_id in self._list_chats():
                    try:
                        with self._session_factory() as session:
                            wm = self._chat_watermark(session, chat_id)
                        if wm is None:
                            continue
                        if self._watermarks.get(chat_id) == wm:
                            continue  # 无新消息,跳过省 LLM
                        with self._session_factory() as session:
                            done = stitch_chat(session, chat_id, self._llm)
                            wm_after = self._chat_watermark(session, chat_id)
                        self._watermarks[chat_id] = wm_after
                        total += len(done)
                        if done:
                            logger.info("会话缝合 %s: 合并 %d 对 %s", chat_id, len(done), done)
                    except Exception:  # noqa: BLE001
                        logger.exception("会话缝合异常 %s", chat_id)
                logger.info("会话缝合轮次完成: 合并 %d 对", total)
                return total
            finally:
                self._release_lock(token)
        finally:
            self._round_lock.release()

    # ============================================================ 后台调度
    def start(self) -> None:
        """启动后台守护线程(幂等)。LLM 未配置或间隔<=0 时 no-op。"""
        if not self.enabled:
            logger.info("会话缝合未启用(LLM 未配置),后台调度不启动")
            return
        settings = get_settings()
        if not settings.conv_stitch_enabled:
            logger.info("会话缝合后台调度已禁用(conv_stitch_enabled=False)")
            return
        if settings.conv_stitch_interval_minutes <= 0:
            logger.info("会话缝合后台调度已禁用(conv_stitch_interval_minutes=0)")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="conv-stitch")
        self._thread.start()
        logger.info("会话缝合后台调度已启动(每 %d 分钟一轮)",
                    settings.conv_stitch_interval_minutes)

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        if self._startup_delay > 0:
            self._stop_evt.wait(self._startup_delay)
        while not self._stop_evt.is_set():
            try:
                self.run_round()
            except Exception:  # noqa: BLE001
                logger.exception("会话缝合轮次异常")
            self._stop_evt.wait(self._interval)


@lru_cache
def get_conv_stitch_service() -> ConvStitchService:
    settings = get_settings()
    llm = None
    if settings.llm_enabled:
        llm = LLMClient(
            settings.llm_api_base,
            settings.llm_api_key,
            settings.llm_model,
            settings.llm_timeout,
        )
    return ConvStitchService(llm=llm)
