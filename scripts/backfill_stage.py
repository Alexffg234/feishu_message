#!/usr/bin/env python3
"""回填处理阶段 stage(与 topic 正交的第二维度)。

把「已推/上门/远程删除…」这类动作词从主题标签里剥离后,处理进展由 stage 承载。
本脚本用纯规则(stage.detect_stage)重算存量每个回复链组的阶段,**不调用 LLM**,
幂等、可反复执行;dry-run 只打印分布不写库。

用法:
    python scripts/backfill_stage.py --dry-run          # 只看阶段分布
    python scripts/backfill_stage.py                    # 全量回填
    python scripts/backfill_stage.py --chat oc_xxx      # 只处理某群
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.core.db import SessionLocal, init_db  # noqa: E402
from app.dao.conversation_repo import ConversationRepo  # noqa: E402
from app.dao.message_repo import MessageRepo  # noqa: E402
from app.models.message import Message  # noqa: E402
from app.services.stage import detect_stage  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="回填回复链组的处理阶段 stage")
    parser.add_argument("--chat", default="", help="只处理指定 chat_id")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写库")
    args = parser.parse_args()

    init_db()

    with SessionLocal() as session:
        stmt = select(Message.chat_id, Message.root_message_id).where(
            Message.root_message_id != ""
        )
        if args.chat:
            stmt = stmt.where(Message.chat_id == args.chat)
        groups = session.execute(stmt.distinct()).all()

        repo = MessageRepo(session)
        convo = ConversationRepo(session)
        dist: Counter[str] = Counter()
        changed = 0
        for chat_id, root_id in groups:
            rows = repo.list_by_root(root_id, chat_id)
            if not rows:
                continue
            stage = detect_stage(m.text for m in rows)
            dist[stage or "(未判定)"] += 1
            if stage:
                changed += 1
            if args.dry_run:
                continue
            repo.set_stage_for_group(chat_id, root_id, stage)
            convo.set_stage(chat_id, root_id, stage)

        if not args.dry_run:
            session.commit()

        print(f"扫描回复链组 {len(groups)} 个,判定出阶段 {changed} 个")
        for name, count in dist.most_common():
            print(f"  {name}: {count}")
        if args.dry_run:
            print("(dry-run,未写库)")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
