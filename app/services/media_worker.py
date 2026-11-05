"""媒体资源异步解析 worker。

背景:事件队列消费者线程(先应答后处理)串行执行消息入库;若入库前同步下载
上传媒体(大视频/大文件可耗时数秒到分钟),会长时间阻塞整个队列(队头阻塞),
拖累后续所有事件。因此媒体解析必须移出消费线程,异步并发完成。

设计:
- 模块级单例 MediaWorker(ThreadPoolExecutor,默认 4 线程),与事件队列同步启停。
- 消息落库时:if 确实新写入且含媒体 → submit((chat_id, message_id, message_type, raw))。
  消费者线程只做快速落库,媒体 URL 字段留空。
- worker 每个 job 独立开 session(不跨线程复用),跑 message_service.resolve_media
  还原 URL(复用现有 _resolve_image/_resolve_video/_resolve_file/_resolve_post_images),
  再经 MessageRepo.update_raw 按 (chat_id, message_id) 回填该消息行的 raw。
- 幂等:七牛 key 用 image_key/file_key 天然去重;回填按行定位,重复执行安全。
- 降级:worker 未启动 / 提交失败 / 解析失败 → URL 留空,绝不阻塞或中断消息落库。
  崩溃窗口(消息已落库但 worker 未及回填 URL)属可接受优雅降级,历史恢复仍由
  scripts/backfill_images.py 承担。
"""
from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache

from app.core.db import SessionLocal
from app.dao.message_repo import MessageRepo

logger = logging.getLogger(__name__)


@dataclass
class MediaJob:
    """一条待异步解析的媒体消息。"""

    chat_id: str
    message_id: str
    message_type: str
    raw: dict
    # 已提交的 Future,供优雅关停时等待在途任务
    future: Future | None = field(default=None, repr=False)


class MediaWorker:
    """媒体解析线程池。start 后 submit 入队,shutdown 优雅排空。"""

    def __init__(
        self,
        max_workers: int = 4,
        session_factory=None,
    ):
        self._max_workers = max_workers
        self._session_factory = session_factory or SessionLocal
        self._executor: ThreadPoolExecutor | None = None
        self._jobs: list[MediaJob] = []

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        """启动线程池(幂等)。"""
        if self._executor is not None:
            return
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers, thread_name_prefix="media-worker"
        )
        logger.info("媒体解析 worker 已启动 threads=%d", self._max_workers)

    def submit(
        self, chat_id: str, message_id: str, message_type: str, raw: dict
    ) -> bool:
        """把一个媒体解析任务入队。返回是否成功入队(失败则 URL 留空,不阻塞)。"""
        if self._executor is None:
            logger.warning("媒体 worker 未启动,跳过异步解析 message_id=%s", message_id)
            return False
        job = MediaJob(chat_id=chat_id, message_id=message_id, message_type=message_type, raw=raw)
        try:
            job.future = self._executor.submit(self._handle, job)
            self._jobs.append(job)
            return True
        except Exception:  # noqa: BLE001 - 池满/关闭时不阻塞入队
            logger.exception("媒体任务提交失败 message_id=%s(URL 留空)", message_id)
            return False

    def shutdown(self, wait: bool = True) -> None:
        """优雅关停:等待在途任务处理完(drain)后释放线程池。"""
        if self._executor is None:
            return
        try:
            self._executor.shutdown(wait=wait)
        finally:
            self._executor = None
        logger.info("媒体解析 worker 已停止")

    # ------------------------------------------------------------ 单 job 处理
    def _handle(self, job: MediaJob) -> None:
        """解析媒体 URL 并回填 DB 中的消息行(独立 session)。失败仅留空。"""
        try:
            # 延迟导入,避免与 message_service 互相 import 造成循环
            from app.services.message_service import resolve_media

            resolved = dict(job.raw)
            resolve_media(resolved, job.message_type, job.message_id)
            with self._session_factory() as session:
                MessageRepo(session).update_raw(job.chat_id, job.message_id, resolved)
            logger.info(
                "媒体解析完成 %s/%s (%s)", job.chat_id, job.message_id, job.message_type
            )
        except Exception:  # noqa: BLE001 - 解析/回填失败不阻塞,URL 留空
            logger.exception(
                "媒体解析失败 chat=%s msg=%s (不影响消息,URL 留空)",
                job.chat_id, job.message_id,
            )


@lru_cache
def get_media_worker() -> MediaWorker:
    """取模块级媒体 worker 单例(每进程一份)。"""
    return MediaWorker()
