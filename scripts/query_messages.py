#!/usr/bin/env python3
"""查询 / 导出数据库中的消息。

用法:
    python scripts/query_messages.py                 # 最近 100 条
    python scripts/query_messages.py --limit 20      # 最近 20 条
    python scripts/query_messages.py --chat oc_xxx   # 按群过滤
    python scripts/query_messages.py --groups        # 按回复链分组输出
    python scripts/query_messages.py --topics        # 按 LLM 主题聚合输出
    python scripts/query_messages.py --reclassify    # 对未分类的组重新调 LLM 打标签
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
from app.services.message_service import MessageService  # noqa: E402


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
        "parent_message_id": m.parent_message_id,
        "root_message_id": m.root_message_id,
        "topic": m.topic,
        "received_at": m.received_at.isoformat() if m.received_at else None,
        "raw": m.raw,
    }


def _print_group_block(g: dict, prefix: str = "   ") -> None:
    """打印单个回复链组(根消息 + 回复)。"""
    print(
        f"{prefix}组 root={g['root_message_id']} @ {g['chat_id']} "
        f"({g['message_count']} 条, 发送者: {', '.join(g['senders']) or '-'})"
    )
    if g["root"] is not None:
        r = g["root"]
        print(
            f"{prefix}  └ 根 [{r.create_time or '-'}] {r.sender_id or '?'}: "
            f"{r.text[:60] or '(非文本)'}"
        )
    for m in g["replies"]:
        print(
            f"{prefix}  └ 回 [{m.create_time or '-'}] {m.sender_id or '?'} "
            f"(回复 {m.parent_message_id}): {m.text[:60] or '(非文本)'}"
        )


def _print_groups(groups: list[dict]) -> None:
    """按回复链分组打印。"""
    print(f"共 {len(groups)} 组")
    print("=" * 60)
    for i, g in enumerate(groups, 1):
        print(f"#{i}")
        _print_group_block(g)
        print("-" * 60)


def _print_topics(topics: list[dict]) -> None:
    """按主题聚合打印:主题 → 回复链组 → 消息。"""
    print(f"共 {len(topics)} 个主题")
    print("=" * 60)
    for i, t in enumerate(topics, 1):
        print(
            f"#{i} 主题: {t['topic']} "
            f"({t['group_count']} 组 / {t['message_count']} 条, "
            f"发送者: {', '.join(t['senders']) or '-'})"
        )
        for g in t["groups"]:
            _print_group_block(g, prefix="   ")
        print("-" * 60)


def _reclassify() -> int:
    """对所有未打主题标签的回复链组,逐组同步调 LLM 分类。"""
    from app.services.topic_service import get_topic_service

    service = get_topic_service()
    if not service.enabled:
        print("LLM 未配置(需设置 LLM_API_KEY / LLM_MODEL),无法重新分类")
        return 1

    with SessionLocal() as session:
        repo = MessageRepo(session)
        roots = repo.list_roots(limit=100000)

    print(f"共 {len(roots)} 组待检查")
    ok = skip = fail = 0
    for root_id, chat_id in roots:
        try:
            label = service.classify_group(chat_id, root_id)
            if label:
                print(f"  {chat_id}/{root_id} -> {label}")
                ok += 1
            else:
                skip += 1
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"  {chat_id}/{root_id} 分类失败: {e}")
    print(f"完成: 新分类 {ok}, 已有标签 {skip}, 失败 {fail}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="查询采集团聊消息")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--chat", default=None, help="按 chat_id 过滤")
    parser.add_argument("--groups", action="store_true", help="按回复链分组输出")
    parser.add_argument("--topics", action="store_true", help="按 LLM 主题聚合输出")
    parser.add_argument(
        "--reclassify", action="store_true", help="对未打标签的组重新调 LLM 分类"
    )
    parser.add_argument("--json", metavar="OUT", help="导出为 JSON 数组")
    args = parser.parse_args()

    if args.reclassify:
        return _reclassify()

    if args.topics:
        from app.services.topic_service import get_topic_service

        topics = get_topic_service().list_topics(
            chat_id=args.chat, limit=args.limit
        )
        _print_topics(topics)
        return 0

    if args.groups:
        with SessionLocal() as session:
            service = MessageService(session)
            groups = service.list_groups(chat_id=args.chat, limit=args.limit)
        _print_groups(groups)
        return 0

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
