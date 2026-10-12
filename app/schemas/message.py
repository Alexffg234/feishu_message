"""消息数据结构 (Pydantic schema)。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class MessageCreate(BaseModel):
    """从飞书事件解析出的待入库消息。"""

    message_id: str
    chat_id: str
    sender_id: str = ""
    sender_type: str = ""
    message_type: str = ""
    text: str = ""
    create_time: str = ""
    raw: dict[str, Any] | None = None


class MessageOut(BaseModel):
    """对外输出的消息表示(用于查询接口)。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    message_id: str
    chat_id: str
    sender_id: str
    sender_type: str
    message_type: str
    text: str
    create_time: str
    received_at: datetime


class StatsOut(BaseModel):
    """系统统计信息。"""

    service: str
    status: str
    messages_count: int
    event_path: str
    database: str
    configured: bool
