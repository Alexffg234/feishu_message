#!/usr/bin/env python3
"""存量回填:按 case_key 把同设备标识的会话软合并(打上相同 case_id,阶段三)。

背景:阶段三起缝合改为软合并(只写 conversations.case_id),但存量库中已入库的
历史会话未跑过软合并。本脚本读取 messages.case_key(阶段二已回填),把
同一 chat 内 case_key 相同(非空)的会话软合并为同一案例组。

幂等:已非空 case_id 的会话跳过;dry-run 只预览不写库;可重复执行。

用法:
    python scripts/backfill_case_id.py --dry-run        # 预览将产生多少案例组
    python scripts/backfill_case_id.py                  # 全量软合并回填
    python scripts/backfill_case_id.py --chat oc_xxx    # 只处理某群
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.core.db import SessionLocal, init_db  # noqa: E402
from app.models.conversation import Conversation  # noqa: E402
from app.models.message import Message  # noqa: E402
from app.services.case_merge import soft_link_cases  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="按 case_key 存量软合并为案例组")
    parser.add_argument("--chat", default="", help="只处理指定 chat_id")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写库")
    args = parser.parse_args()

    init_db()

    with SessionLocal() as session:
        # 该群未关联(case_id=0)且含非空 case_key 的会话,(chat, key) -> [conv_id]
        group_rows = session.execute(
            select(Message.chat_id, Message.case_key, Message.conversation_id)
            .where(Message.case_key != "", Message.conversation_id != 0)
            .distinct()
            .order_by(Message.chat_id, Message.case_key)
        ).all()
        # 只保留 case_id=0 的会话,并存 (chat,key) -> 排序后的 [conv_id]
        unlinked: dict[tuple[str, str], list[int]] = {}
        for chat_id, key, cid in group_rows:
            if cid is None or cid == 0:
                continue
            unlinked.setdefault((chat_id, key), set()).add(int(cid))
        if args.chat:
            unlinked = {
                (c, k): v for (c, k), v in unlinked.items() if c == args.chat
            }

        total_groups = 0
        total_linked = 0
        for (chat_id, case_key), cid_set in sorted(unlinked.items()):
            convs = (
                session.query(Conversation)
                .filter(
                    Conversation.chat_id == chat_id,
                    Conversation.id.in_(cid_set),
                )
                .order_by(Conversation.id)
                .all()
            )
            if len(convs) < 2:
                continue
            target_id = convs[0].id
            for c in convs[1:]:
                if args.dry_run:
                    total_linked += 1
                    continue
                if soft_link_cases(session, chat_id, c.id, target_id):
                    total_linked += 1
            total_groups += 1
        if not args.dry_run and total_linked:
            session.commit()
    print(
        f"命中 {len(unlinked)} 个设备标识,可软合并 {total_groups} 个案例组,"
        f"关联 {total_linked} 个后续会话"
        + (" (dry-run 未写库)" if args.dry_run else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
