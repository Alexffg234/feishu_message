"""飞书事件分发器。

构造 lark-oapi 的 EventDispatcher(自动完成签名校验、加密解密),
并把 im.message.receive_v1 事件路由到消息队列(先应答后处理):

- 回调只做「入队」,毫秒级返回,飞书立即收到 200;
- 后台单消费者线程串行执行入库/媒体下载/主题分类(见 event_queue);
- 队列满时降级为同步处理(慢但不丢,与旧行为一致);
- message_id 唯一索引保证飞书重推/重试幂等。
"""
from __future__ import annotations

import logging
from functools import lru_cache

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from app.core.config import BASE_DIR, get_settings
from app.services.event_queue import EventQueue
from app.services.message_service import MessageService
from app.services.topic_service import get_topic_service

logger = logging.getLogger(__name__)


def _on_receive(data: P2ImMessageReceiveV1, session_factory) -> None:
    """处理单条事件:用独立 Session 处理,避免跨事件共享事务。

    作为队列 process_fn 使用;抛出的异常由队列退避重试,耗尽写死信。
    """
    settings = get_settings()
    with session_factory() as session:
        service = MessageService(
            session,
            allowed_chat_ids=settings.allowed_chat_id_set,
            topic_scheduler=get_topic_service().schedule_group,
        )
        service.handle_receive_event(data)


@lru_cache
def get_event_queue() -> EventQueue:
    """进程级事件队列单例(每个 gunicorn worker 一份)。"""
    return EventQueue(
        process_fn=lambda data: _on_receive(data, _session_factory()),
        maxsize=1000,
        dead_letter=str(BASE_DIR / "data" / "dead_events.jsonl"),
        retry_delays=(5.0, 30.0, 120.0),
    )


def _session_factory():
    # 延迟导入避免模块加载顺序问题(测试可直接注入假工厂)
    from app.core.db import SessionLocal

    return SessionLocal


def build_dispatcher(session_factory, event_queue: EventQueue | None = None) -> lark.EventDispatcherHandler:
    """构造事件分发器。

    session_factory: 降级同步处理时的 Session 工厂。
    event_queue: 事件队列,默认进程单例;测试可注入独立实例。
    """
    settings = get_settings()
    queue = event_queue if event_queue is not None else get_event_queue()

    def handler(data: P2ImMessageReceiveV1) -> None:
        if queue.submit(data):
            return
        # 队列满/已停止:降级同步处理(慢但不丢)
        logger.warning("事件队列不可用,降级同步处理")
        _on_receive(data, session_factory)

    return (
        # 注意: SDK builder 签名是 builder(encrypt_key, verification_token)
        # 顺序不可颠倒,否则 token 校验会失败
        lark.EventDispatcherHandler.builder(
            settings.encrypt_key, settings.verification_token
        )
        .register_p2_im_message_receive_v1(handler)
        .build()
    )
