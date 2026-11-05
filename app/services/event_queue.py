"""先应答后处理:事件队列与单消费者线程(outbox 持久化)。

背景:飞书事件回调要求快速应答。若媒体下载/DB 写入慢,应答被拖到飞书侧
超时,飞书会重推;全部重推超时则事件丢失,worker 重启也会丢在途事件。
因此回调解密校验后立即入队应答 200,由单消费者线程后台串行处理。

可靠性(至少一次,outbox 模式):
- session_factory 注入时走「outbox 持久化」:submit 先把事件原子写入
  event_outbox 表(pending),落库成功才返回(飞书收到 200);单消费者线程
  从表中取 pending 事件处理,成功后标 done。worker 崩溃 / gunicorn 优雅
  重启时,未处理事件仍留在表中,重启后恢复处理,不再因纯内存队列丢失。
- event_key 唯一键:优先事件自带 id(header.event_id),飞书消息事件回退用
  (chat_id, message_id),与消息入库幂等键一致,天然防重推(SQLite/PG/MySQL
  靠唯一约束 + IntegrityError 幂等吞并)。
- 降级:outbox 写入失败(DB 短暂不可用)时,退回纯内存队列(慢但不丢),
  由同一消费者线程一并排空。

失败处理:process_fn 抛异常按 retry_delays 退避重试,耗尽后写死信
(JSONL 追加),由回补脚本/人工恢复,不阻塞后续事件。

H6(重试饥饿修复):退避重试不再只在队列恰好为空时才刷新——每次消费后
也带超时刷新,保证队列持续繁忙时重试任务仍能被调度,不被无限期饿死;
outbox 模式下重试落在表内 retrying 行,按 retry_at_ms 定时可取,天然无饥饿。
"""
from __future__ import annotations

import hashlib
import json
import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.models.event_outbox import EventOutbox

logger = logging.getLogger(__name__)

_SENTINEL = object()

# 认领出的 outbox 行快照(会话已关闭,供处理/标记阶段使用)
@dataclass
class _ClaimedRow:
    id: int
    event_key: str
    payload: str
    attempt: int

# 内存模式下退避重试的批量刷新窗口(秒);outbox 模式下为轮询间隔
_EMPTY_WAIT = 0.2
# 单次消费后刷新退避重试的阻塞窗口(H6:避免重试饥饿)
_FLUSH_WAIT = 0.05


def _extract_event_id(item: Any) -> str | None:
    """提取事件自带 id(header.event_id);拿不到返回 None。"""
    to_dict = getattr(item, "to_dict", None)
    if not callable(to_dict):
        return None
    try:
        data = to_dict()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    header = data.get("header") or {}
    event_id = header.get("event_id")
    return str(event_id) if event_id else None


def _deep_get(data: Any, dotted: str) -> str | None:
    """按 'a:b:c' 逐层取 dict/对象属性的字符串值;失败返回 None。"""
    cur: Any = data
    for part in dotted.split(":"):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            cur = getattr(cur, part, None)
        if cur is None:
            return None
    return str(cur) if not isinstance(cur, (dict, list)) else None


def _event_key(item: Any) -> str:
    """生成 outbox 去重键。

    优先事件自带 id(header.event_id);飞书消息事件回退 (chat_id, message_id);
    兜底对 payload 稳定哈希,保证任何类型都能入表且重复提交可去重。
    """
    event_id = _extract_event_id(item)
    if event_id:
        return f"event:{event_id}"
    chat_id = _deep_get(item, "event:message:chat_id")
    message_id = _deep_get(item, "event:message:message_id")
    if chat_id and message_id:
        return f"{chat_id}:{message_id}"
    digest = hashlib.sha256(
        json.dumps(_event_to_dict(item), sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return f"hash:{digest}"


def _lark_to_plain(value: Any) -> Any:
    """把 lark 模型对象深度展开为普通 Python 结构。

    lark-oapi 的模型对象无 to_dict(),数据以 setattr 存于 __dict__(含 _types
    类型映射)。据此递归还原成 dict/list,供 JSON 序列化与还原。
    """
    if isinstance(value, (list, tuple)):
        return [_lark_to_plain(i) for i in value]
    if isinstance(value, dict):
        return {k: _lark_to_plain(x) for k, x in value.items()}
    if hasattr(value, "_types") and hasattr(value, "__dict__"):
        out: dict[str, Any] = {}
        for key, sub in value.__dict__.items():
            if key == "_types":
                continue
            out[key] = _lark_to_plain(sub)
        return out
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _event_to_dict(item: Any) -> Any:
    """尽量把事件序列化成 dict(存储/死信恢复用);失败降级为 repr。

    优先底层 to_dict();lark 模型对象(无 to_dict)走 _lark_to_plain 深度展开;
    兜底 repr。
    """
    if isinstance(item, (dict, list, str, int, float, bool)) or item is None:
        return item
    to_dict = getattr(item, "to_dict", None)
    if callable(to_dict):
        try:
            res = to_dict()
            if not isinstance(res, str):
                return res
        except Exception:  # noqa: BLE001
            pass
    try:
        plain = _lark_to_plain(item)
        if isinstance(plain, dict):
            return plain
    except Exception:  # noqa: BLE001
        pass
    return repr(item)


def _event_to_payload(item: Any) -> str:
    """序列化为 JSON 字符串;带类型标记供消费端反序列化还原对象。"""
    return json.dumps(
        {"_type": type(item).__name__, "data": _event_to_dict(item)},
        ensure_ascii=False,
    )


def _payload_to_item(payload: str) -> Any:
    """反序列化 outbox 里的 payload。

    仅对已知的 SDK 事件(im 消息事件)按原样还原;其余按 dict 交给 process_fn。
    """
    obj = json.loads(payload)
    if not isinstance(obj, dict) or "_type" not in obj:
        return obj
    type_name = obj["_type"]
    if type_name == "P2ImMessageReceiveV1":
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

        return P2ImMessageReceiveV1(obj["data"])
    return obj.get("data")


class EventQueue:
    """有界事件队列 + 单消费者线程(outbox 持久化/内存双模式)。

    注入 session_factory 时为持久化模式(at-least-once);否则为纯内存模式
    (测试/无 DB 环境)。失败退避重试,耗尽写死信。
    """

    def __init__(
        self,
        process_fn: Callable[[Any], None],
        maxsize: int = 1000,
        dead_letter: str = "",
        retry_delays: tuple[float, ...] = (5.0, 30.0, 120.0),
        session_factory: Callable | None = None,
        claim_lease_ms: int = 60_000,
    ) -> None:
        self._process_fn = process_fn
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)
        self._dead_letter = dead_letter
        self._retry_delays = tuple(retry_delays)
        # [(ready_at_monotonic, item, attempt), ...] 内存模式下退避中等待重试的事件
        self._pending_retry: list[tuple[float, Any, int]] = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stopping = False
        # ---- outbox 持久化相关 ----
        self._session_factory = session_factory
        self._persistent = session_factory is not None
        # 处理租约时长(毫秒):processing 状态的 row 超过此时间未完成即视为可回收
        self._claim_lease_ms = claim_lease_ms

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping = False
        self._thread = threading.Thread(
            target=self._run, name="event-queue-consumer", daemon=True
        )
        self._thread.start()
        logger.info(
            "事件队列消费者已启动 maxsize=%d persistent=%s",
            self._q.maxsize,
            self._persistent,
        )

    def stop(self, drain: bool = True) -> None:
        """停止消费。drain=True 时先处理完已入队事件再退出。"""
        self._stopping = True
        if self._thread is None:
            return
        self._q.put(_SENTINEL)
        self._thread.join(timeout=60 if drain else 5)
        self._thread = None
        logger.info("事件队列消费者已停止")

    # ------------------------------------------------------------ 提交
    def submit(self, item: Any) -> bool:
        """非阻塞入队。先写 outbox(持久化模式),失败/满则降级内存队列。

        返回 True 表示事件已接受(已落库 或 已进内存队列);返回 False 仅当
        队列满且持续停止(调用方降级同步处理)。
        """
        if self._stopping:
            return False
        if self._persistent and self._submit_outbox(item):
            return True
        if self._persistent:
            logger.warning("outbox 写入失败,降级内存队列")
        try:
            self._q.put_nowait(item)
            return True
        except queue.Full:
            logger.warning("事件队列已满 maxsize=%d", self._q.maxsize)
            return False

    def _submit_outbox(self, item: Any) -> bool:
        """把事件原子写入 event_outbox(pending)。重复 key 幂等吞并。"""
        key = _event_key(item)
        payload = _event_to_payload(item)
        try:
            with self._session_factory() as session:
                session.add(
                    EventOutbox(event_key=key, payload=payload, status="pending")
                )
                session.commit()
            logger.info("事件已写入 outbox key=%s", key)
            return True
        except IntegrityError:
            # 飞书重推/重复提交:唯一键已存在,幂等成功
            logger.info("事件已存在于 outbox key=%s,幂等处理", key)
            return True
        except Exception:  # noqa: BLE001
            logger.exception("outbox 写入失败 key=%s", key)
            return False

    def qsize(self) -> int:
        return self._q.qsize()

    # ------------------------------------------------------------ 消费循环
    def _run(self) -> None:
        if self._persistent:
            self._run_persistent()
            return
        self._run_memory()

    def _run_memory(self) -> None:
        """纯内存模式消费循环;H6:每次消费后也刷新退避重试,避免饥饿。"""
        while True:
            try:
                item = self._q.get(timeout=_EMPTY_WAIT)
            except queue.Empty:
                if self._stopping:
                    break
                self._flush_due_retries()
                continue
            self._q.task_done()
            if item is _SENTINEL:
                break
            self._process(item, 0)
            # H6:队列持续繁忙时,退避重试不再饿死————消费后带超时刷新
            self._flush_due_retries(timeout=_FLUSH_WAIT)
        # 排空剩余(drain),避免优雅关停时丢在途事件
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item is not _SENTINEL:
                self._process(item, 0)
        self._flush_due_retries(block=True)

    def _run_persistent(self) -> None:
        """outbox 模式消费循环:轮询取 pending/到期 retrying 行处理。

        retrying 到期时间落在表里,按 retry_at_ms 定时可取,天然不存在
        内存模式的饥饿问题;H6 因此自动满足。
        """
        while not self._stopping:
            # 先排空降级期间进入内存队列的事件(DB 不可用时的尽力而为)
            self._drain_memory()
            row = self._claim_next()
            if row is None:
                if self._stopping:
                    break
                time.sleep(_EMPTY_WAIT)
                continue
            self._process_outbox(row)

    def _drain_memory(self) -> bool:
        """尽力处理内存队列中的事件(降级产物)。返回是否处理过。"""
        handled = False
        while not self._stopping:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            self._q.task_done()
            if item is _SENTINEL:
                break
            self._process(item, 0)
            handled = True
        return handled

    # ------------------------------------------------------------ outbox 处理
    def _claim_next(self) -> "_ClaimedRow | None":
        """原子认领一个可处理行:pending / 到期 retrying / 过期 processing。

        用 UPDATE ... WHERE status/retry_at 条件做乐观守卫,多 worker 并发时
        只有一个能抢到(rowcount=1),其余抢锁失败返回 None(交给下次轮询)。

        processing 行在 retry_at_ms(处理租约到期时刻)之后视为疑似崩溃,可回收
        重新认领,保证 worker 崩溃时不丢处理中的事件。认领时保留原 attempt。

        返回轻量记录(会话已关闭,避免 DetachedInstanceError),字段与 ORM 行对齐。
        """
        now = int(time.time() * 1000)
        with self._session_factory() as session:
            row = (
                session.query(EventOutbox)
                .filter(
                    EventOutbox.status.in_(["pending", "retrying", "processing"]),
                    EventOutbox.retry_at_ms <= now,
                )
                .order_by(EventOutbox.id)
                .limit(1)
                .with_for_update(skip_locked=True)
                .first()
            )
            if row is None:
                session.commit()
                return None
            result = session.execute(
                update(EventOutbox)
                .where(
                    EventOutbox.id == row.id,
                    EventOutbox.status.in_(["pending", "retrying", "processing"]),
                    EventOutbox.retry_at_ms <= now,
                )
                .values(
                    status="processing",
                    retry_at_ms=now + self._claim_lease_ms,
                )
            )
            session.commit()
            if result.rowcount != 1:
                return None  # 抢锁失败(其他 worker 已处理)
            return _ClaimedRow(
                id=row.id,
                event_key=row.event_key,
                payload=row.payload,
                attempt=row.attempt,
            )

    def _process_outbox(self, row: "_ClaimedRow") -> None:
        try:
            data = _payload_to_item(row.payload)
            self._process_fn(data)
        except Exception as exc:  # noqa: BLE001
            logger.exception("事件处理失败 key=%s(第 %d 次)", row.event_key, row.attempt + 1)
            self._mark_retry_or_failed(row, exc)
            return
        self._mark_done(row)

    def _mark_done(self, row: "_ClaimedRow") -> None:
        try:
            with self._session_factory() as session:
                session.execute(
                    update(EventOutbox)
                    .where(EventOutbox.id == row.id)
                    .values(status="done", retry_at_ms=0)
                )
                session.commit()
            logger.info("事件处理完成 key=%s", row.event_key)
        except Exception:  # noqa: BLE001
            logger.exception("outbox 标记 done 失败 key=%s", row.event_key)

    def _mark_retry_or_failed(self, row: "_ClaimedRow", exc: Exception) -> None:
        delay_idx = row.attempt
        if delay_idx < len(self._retry_delays):
            delay = self._retry_delays[delay_idx]
            retry_at = int(time.time() * 1000) + int(delay * 1000)
            try:
                with self._session_factory() as session:
                    session.execute(
                        update(EventOutbox)
                        .where(EventOutbox.id == row.id)
                        .values(
                            status="retrying",
                            attempt=row.attempt + 1,
                            retry_at_ms=retry_at,
                        )
                    )
                    session.commit()
                logger.warning(
                    "事件已安排重试 key=%s 第 %d 次,延时 %.1fs",
                    row.event_key,
                    row.attempt + 1,
                    delay,
                )
            except Exception:  # noqa: BLE001
                logger.exception("outbox 标记 retrying 失败 key=%s", row.event_key)
            return
        # 重试耗尽:标记 failed 并写死信
        self._mark_failed(row, exc)

    def _mark_failed(self, row: "_ClaimedRow", exc: Exception) -> None:
        try:
            with self._session_factory() as session:
                session.execute(
                    update(EventOutbox)
                    .where(EventOutbox.id == row.id)
                    .values(status="failed", retry_at_ms=0)
                )
                session.commit()
        except Exception:  # noqa: BLE001
            logger.exception("outbox 标记 failed 失败 key=%s", row.event_key)
        try:
            item = _payload_to_item(row.payload)
        except Exception:  # noqa: BLE001
            item = row.payload
        self._write_dead_letter(item, error=f"重试耗尽: {exc}")

    # ------------------------------------------------------------ 重试/死信(内存 / 退化共用)
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

    def _flush_due_retries(self, timeout: float = 0.0, block: bool = False) -> None:
        """把到期的内存退避重试重新调度。

        H6 修复点:调用方(消费后/队列空时)带 timeout 调用,保证退避中的
        重试任务即便在队列持续繁忙时也能按期被处理,不被饿死。
        """
        now = time.monotonic()
        with self._lock:
            if block:
                due = list(self._pending_retry)
                self._pending_retry.clear()
            else:
                due = [p for p in self._pending_retry if p[0] <= now]
                self._pending_retry = [p for p in self._pending_retry if p[0] > now]
        for _ready, item, attempt in due:
            self._process(item, attempt, final=block)

    def _write_dead_letter(self, item: Any, error: str = "重试耗尽") -> None:
        if not self._dead_letter:
            logger.error("事件重试耗尽且未配置死信文件,事件丢弃: %r", item)
            return
        payload = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "error": error,
            "event": _event_to_dict(item),
        }
        try:
            with open(self._dead_letter, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            logger.error("事件写入死信 %s", self._dead_letter)
        except OSError:
            logger.exception("死信文件写入失败 %s", self._dead_letter)
