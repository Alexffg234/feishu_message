#!/usr/bin/env python3
"""查询对话关系(conversations / message_links)。

用法:
    python scripts/query_conversations.py                 # 最近 50 个对话
    python scripts/query_conversations.py --limit 10
    python scripts/query_conversations.py --chat oc_xxx   # 按群过滤
    python scripts/query_conversations.py --tree <conv_id>  # 查看某对话的完整回复树
    python scripts/query_conversations.py --json out.json  # 导出为 JSON
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import SessionLocal  # noqa: E402
from app.dao.conversation_repo import ConversationRepo  # noqa: E402


def _conv_dict(c) -> dict:
    return {
        "id": c.id,
        "chat_id": c.chat_id,
        "root_message_id": c.root_message_id,
        "root_sender_id": c.root_sender_id,
        "status": c.status,
        "topic": c.topic,
        "message_count": c.message_count,
        "participant_count": c.participant_count,
        "last_message_at_ms": c.last_message_at_ms,
    }


def _msg_dict(m) -> dict:
    return {
        "message_id": m.message_id,
        "sender_id": m.sender_id,
        "text": (m.text or "")[:60],
    }


def _tree_dict(node) -> dict:
    msg = node["message"]
    return {
        "message": _msg_dict(msg) if msg else {"message_id": "(未采集)"},
        "children": [_tree_dict(c) for c in node["children"]],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="查询对话关系")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--chat", default=None)
    parser.add_argument("--tree", type=int, default=None, help="查看某 conversation id 的回复树")
    parser.add_argument("--json", metavar="OUT", help="导出 JSON")
    args = parser.parse_args()

    with SessionLocal() as session:
        repo = ConversationRepo(session)
        if args.tree is not None:
            tree = repo.list_tree(args.tree)
            data = {
                "conversation_id": args.tree,
                "roots": [_tree_dict(r) for r in tree["roots"]],
                "message_count": tree["message_count"],
            }
        else:
            convs = repo.list_recent(chat_id=args.chat, limit=args.limit)
            data = [_conv_dict(c) for c in convs]

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        print(f"已导出 -> {args.json}")
        return 0

    if args.tree is not None:
        print(f"对话 #{args.tree} 回复树(共 {data['message_count']} 条):")
        print(json.dumps(data["roots"], ensure_ascii=False, indent=2))
        return 0

    print(f"共 {len(data)} 个对话")
    for c in data:
        print(
            f"[{c['id']}] {c['topic'] or '(未分类)'} | {c['root_message_id']} | "
            f"{c['message_count']}条/{c['participant_count']}人"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
