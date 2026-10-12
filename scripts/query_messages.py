#!/usr/bin/env python3
"""查询 / 导出数据库中的消息。

用法:
    python scripts/query_messages.py                 # 最近 100 条
    python scripts/query_messages.py --limit 20      # 最近 20 条
    python scripts/query_messages.py --chat oc_xxx   # 按群过滤
    python scripts/query_messages.py --json out.json # 导出为 JSON
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import SessionLocal  # noqa: E402
from app.dao.message_repo import MessageRepo  # noqa: E402
from app.models.message import Message  # noqa: E402


def _to_dict(m: Message) -> dict:
    return {
        "id": m.id,
        "message_id": m.message_id,
        "chat_id": m.chat_id,
        "sender_id": m.sender_id,
        "sender_type": m.sender_type,
        "message_type": m.message_type,
        "text": m.text,
        "create_time": m.create_time,
        "received_at": m.received_at.isoformat() if m.received_at else None,
        "raw": m.raw,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="查询采集团聊消息")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--chat", default=None, help="按 chat_id 过滤")
    parser.add_argument("--json", metavar="OUT", help="导出为 JSON 数组")
    args = parser.parse_args()

    with SessionLocal() as session:
        repo = MessageRepo(session)
        if args.chat:
            rows = repo.list_by_chat(args.chat, limit=args.limit)
        else:
            rows = repo.list_recent(limit=args.limit)
        data = [_to_dict(r) for r in rows]

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        print(f"已导出 {len(data)} 条 -> {args.json}")
        return 0

    print(f"共 {len(data)} 条")
    print("-" * 60)
    for d in data:
        print(
            f"[{d['create_time'] or '-'}] "
            f"{d['sender_id'] or '?'} @ {d['chat_id']} "
            f"({d['message_type']}): {d['text'][:60] or '(非文本)'}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
