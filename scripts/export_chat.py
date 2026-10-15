#!/usr/bin/env python3
"""把指定群的消息/会话/回复边只读导出到本地 sqlite 文件。

用途:会话缝合(stitch_conversations.py)等离线脚本的本地验证——
在本地副本上改,真实库不动。

用法:
    python scripts/export_chat.py --chat oc_xxx --out data/local.sqlite
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app.core.db import SessionLocal  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.models.conversation import Conversation, MessageLink  # noqa: E402
from app.models.message import Base, Message  # noqa: E402


def _copy(src: object, model) -> object:
    """按列复制(保留原主键 id),便于本地与原库 id 对齐。"""
    data = {c.key: getattr(src, c.key) for c in model.__table__.columns}
    return model(**data)


def main() -> int:
    parser = argparse.ArgumentParser(description="导出指定群数据到本地 sqlite")
    parser.add_argument("--chat", required=True, help="群 chat_id")
    parser.add_argument("--out", required=True, help="本地 sqlite 文件路径")
    args = parser.parse_args()

    setup_logging()
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    local_engine = create_engine(f"sqlite:///{out_path}")
    Base.metadata.create_all(local_engine)
    Local = sessionmaker(bind=local_engine)

    with SessionLocal() as src, Local() as dst:
        msgs = (
            src.query(Message).filter(Message.chat_id == args.chat).order_by(Message.id.asc()).all()
        )
        convs = (
            src.query(Conversation)
            .filter(Conversation.chat_id == args.chat)
            .order_by(Conversation.id.asc())
            .all()
        )
        conv_ids = [c.id for c in convs]
        links = (
            src.query(MessageLink)
            .filter(MessageLink.conversation_id.in_(conv_ids or [0]))
            .order_by(MessageLink.id.asc())
            .all()
        )

        for m in msgs:
            dst.add(_copy(m, Message))
        for c in convs:
            dst.add(_copy(c, Conversation))
        for l in links:
            dst.add(_copy(l, MessageLink))
        dst.commit()

    print(f"导出完成 -> {out_path}")
    print(f"  messages: {len(msgs)} 条")
    print(f"  conversations: {len(convs)} 个")
    print(f"  message_links: {len(links)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
