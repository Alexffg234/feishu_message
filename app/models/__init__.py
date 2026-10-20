"""ORM 模型包。

导入本包会触发所有模型的注册(映射到同一个 Base.metadata),
init_db 依赖此行为来建表。
"""
from app.models.message import Base, Message  # noqa: F401
from app.models.conversation import Conversation, MessageLink  # noqa: F401
from app.models.segment import MessageSegment  # noqa: F401
from app.models.user_name import UserName  # noqa: F401
from app.models.topic_summary import TopicSummary  # noqa: F401
from app.models.topic_group import TopicGroup  # noqa: F401
from app.models.topic_knowledge import TopicKnowledge  # noqa: F401
from app.models.app_lock import SchedulerLock  # noqa: F401

__all__ = [
    "Base",
    "Message",
    "Conversation",
    "MessageLink",
    "MessageSegment",
    "UserName",
    "TopicSummary",
    "TopicGroup",
    "TopicKnowledge",
    "SchedulerLock",
]
