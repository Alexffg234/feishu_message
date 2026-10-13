"""ORM 模型包。

导入本包会触发所有模型的注册(映射到同一个 Base.metadata),
init_db 依赖此行为来建表。
"""
from app.models.message import Base, Message  # noqa: F401
from app.models.conversation import Conversation, MessageLink  # noqa: F401
from app.models.user_name import UserName  # noqa: F401

__all__ = ["Base", "Message", "Conversation", "MessageLink", "UserName"]
