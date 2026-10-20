"""先应答后处理:事件队列与单消费者线程。

背景:飞书事件回调要求快速应答。若媒体下载/DB 写入慢,应答被拖到飞书侧
超时,飞书会重推;全部重推超时则事件丢失,worker 重启也会丢在途事件。
因此回调解密校验后立即入队应答 200,由单消费者线程后台串行处理
(FIFO,同 worker 内保持事件顺序);message_id 唯一索引保证飞书重推幂等。

失败处理:process_fn 抛异常按 retry_delays 退避重试,耗尽后写死信
(JSONL 追加),由回补脚本/人工恢复,不阻塞后续事件。
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

_SENTINEL = object()


class EventQueue:
    """有界事件队列 + 单消费者线程,失败退避重试,耗尽写死信。"""

    def __init__(
        self,
        process_fn: Callable[[Any], None],
        maxsize: int = 1000,
        dead_letter: str = "",
        retry_delays: tuple[float, ...] = (5.0, 30.0, 120.0),
    ) -> None:
        self._process_fn = process_fn
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)
        self._dead_letter = dead_letter
        self._retry_delays = tuple(retry_delays)
        # [(ready_at_monotonic, item, attempt), ...] 退避中等待重试的事件
        self._pending_retry: list[tuple[float, Any, int]] = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stopping = False

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping = False
        self._thread = threading.Thread(
            target=self._run, name="event-queue-consumer", daemon=True
        )
        self._thread.start()
        logger.info("事件队列消费者已启动 maxsize=%d", self._q.maxsize)

    def stop(self, drain: bool = True) -> None:
        """停止消费。drain=True 时先处理完队列中已有事件再退出。"""
        self._stopping = True
        if self._thread is None:
            return
        self._q.put(_SENTINEL)
        self._thread.join(timeout=60 if drain else 5)
        self._thread = None
        logger.info("事件队列消费者已停止")

    # ------------------------------------------------------------ 提交
    def submit(self, item: Any) -> bool:
        """非阻塞入队。队列满或已停止返回 False(调用方自行降级)。"""
        if self._stopping:
            return False
        try:
            self._q.put_nowait(item)
            return True
        except queue.Full:
            logger.warning("事件队列已满 maxsize=%d", self._q.maxsize)
            return False

    def qsize(self) -> int:
        return self._q.qsize()

    # ------------------------------------------------------------ 消费循环
    def _run(self) -> None:
        while True:
            try:
                item = self._q.get(timeout=0.2)
            except queue.Empty:
                if self._stopping:
                    break
                self._flush_due_retries()
                continue
            self._q.task_done()
            if item is _SENTINEL:
                break
            self._process(item, 0)
        # 排空剩余(drain),避免优雅关停时丢在途事件
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item is not _SENTINEL:
                self._process(item, 0)
        self._flush_due_retries(block=True)

    def _flush_due_retries(self, block: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            due = [p for p in self._pending_retry if p[0] <= now]
            if block:
                due = list(self._pending_retry)
                self._pending_retry.clear()
            else:
                self._pending_retry = [p for p in self._pending_retry if p[0] > now]
        for _ready, item, attempt in due:
            self._process(item, attempt, final=block)

    def _process(self, item: Any, attempt: int, final: bool = False) -> None:
        try:
            self._process_fn(item)
            return
        except Exception:  # noqa: BLE001
            logger.exception("事件处理失败(第 %d 次)", attempt + 1)
            if not final and attempt < len(self._retry_delays):
                self._schedule_retry(item, self._retry_delays[attempt], attempt + 1)
                return
            self._write_dead_letter(item)

    def _schedule_retry(self, item: Any, delay: float, attempt: int) -> None:
        with self._lock:
            self._pending_retry.append((time.monotonic() + delay, item, attempt))

    def _write_dead_letter(self, item: Any) -> None:
        if not self._dead_letter:
            logger.error("事件重试耗尽且未配置死信文件,事件丢弃: %r", item)
            return
        payload = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "error": "重试耗尽",
            "event": _event_to_dict(item),
        }
        try:
            with open(self._dead_letter, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            logger.error("事件写入死信 %s", self._dead_letter)
        except OSError:
            logger.exception("死信文件写入失败 %s", self._dead_letter)


def _event_to_dict(item: Any) -> Any:
    """尽量把事件序列化成 dict(死信恢复用);失败降级为 repr。"""
    if isinstance(item, (dict, list, str, int, float, bool)) or item is None:
        return item
    to_dict = getattr(item, "to_dict", None)
    if callable(to_dict):
        try:
            return to_dict()
        except Exception:  # noqa: BLE001
            pass
    return repr(item)
