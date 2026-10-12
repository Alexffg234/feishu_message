"""飞书事件分发器。

构造 lark-oapi 的 EventDispatcher(自动完成签名校验、加密解密),
并把 im.message.receive_v1 事件路由到 MessageService。
Session 由调用方(API 层)通过依赖注入提供。
"""
from __future__ import annotations

import logging

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from app.services.message_service import MessageService
from app.core.config import get_settings

logger = logging.getLogger(__name__)


def _on_receive(data: P2ImMessageReceiveV1, session_factory) -> None:
    """事件回调:用独立 Session 处理每条消息,避免跨事件共享事务。"""
    settings = get_settings()
    with session_factory() as session:
        service = MessageService(
            session, allowed_chat_ids=settings.allowed_chat_id_set
        )
        try:
            service.handle_receive_event(data)
        except Exception:  # noqa: BLE001
            # 单条消息失败不应中断服务;已由 service 内部 rollback
            logger.exception("处理消息事件异常")


def build_dispatcher(session_factory) -> lark.EventDispatcherHandler:
    """构造事件分发器。session_factory 是返回 Session 的可调用对象。"""
    settings = get_settings()

    def handler(data: P2ImMessageReceiveV1) -> None:
        _on_receive(data, session_factory)

    dispatcher = (
        # 注意: SDK builder 签名是 builder(encrypt_key, verification_token)
        # 顺序不可颠倒,否则 token 校验会失败
        lark.EventDispatcherHandler.builder(
            settings.encrypt_key, settings.verification_token
        )
        .register_p2_im_message_receive_v1(handler)
        .build()
    )
    return dispatcher
