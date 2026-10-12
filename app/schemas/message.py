"""消息数据结构 (Pydantic schema)。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

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
    # 回复关系(事件 message.parent_id / root_id);空=独立消息
    parent_message_id: str = ""
    root_message_id: str = ""
    raw: Optional[dict[str, Any]] = None


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
    parent_message_id: str = ""
    root_message_id: str = ""
    topic: str = ""
    received_at: datetime


class MessageGroupOut(BaseModel):
    """消息分组:同一回复链的一组消息(根消息 + 各条回复)。

    A 发消息、B 回复 A、C 再回复 B,三者归为同一组,root_message_id 相同。
    """

    root_message_id: str
    chat_id: str
    # 根消息本身(若根消息未被采集到,如机器人入群前的消息,则为 None)
    root: Optional[MessageOut] = None
    replies: list[MessageOut] = []
    message_count: int = 0
    # 组内出现过的发送者(去重,保持出现顺序)
    senders: list[str] = []


class TopicOut(BaseModel):
    """主题分组:同一主题下的全部回复链组。

    如「投屏」主题下,聚合所有讨论投屏的问答组。
    """

    topic: str
    group_count: int = 0
    message_count: int = 0
    # 主题下出现过的发送者(去重,保持出现顺序)
    senders: list[str] = []
    groups: list[MessageGroupOut] = []


class StatsOut(BaseModel):
    """系统统计信息。"""

    service: str
    status: str
    messages_count: int
    event_path: str
    database: str
    configured: bool
