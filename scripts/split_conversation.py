#!/usr/bin/env python3
"""会话拆分脚本:merge_groups 的逆操作,把被同标签归并混在一起的对话拆回多组。

按计划 JSON 把某对话的消息重新分组:keep 组保留原对话行,其余组各建新对话;
回复边跟随子消息迁移。默认 dry-run 只校验报告;--apply 才落库。

计划 JSON 格式(数组,必须精确覆盖该对话全部消息):
    [
      {"root_message_id": "om_x", "topic": "保留组主题", "keep": true,
       "message_ids": ["om_x", ...]},
      {"root_message_id": "om_y", "topic": "新组主题",
       "message_ids": ["om_y", ...]}
    ]

用法:
    python scripts/split_conversation.py --chat oc_x --conversation 92 --plan plan.json
    python scripts/split_conversation.py --chat oc_x --conversation 92 --plan plan.json --apply
    python scripts/split_conversation.py --db data/local.sqlite --chat oc_x --conversation 92 --plan plan.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.core.db import SessionLocal  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.services.conversation_split import split_conversation  # noqa: E402


def _make_session_factory(db_path: str | None):
    if db_path:
        engine = create_engine(f"sqlite:///{db_path}")
        return sessionmaker(bind=engine)
    return SessionLocal


def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="会话拆分(同主题归并的逆操作)")
    parser.add_argument("--chat", required=True, help="群 chat_id")
    parser.add_argument("--conversation", required=True, type=int, help="要拆分的对话 id")
    parser.add_argument("--plan", required=True, help="拆分计划 JSON 文件")
    parser.add_argument("--db", default=None, help="本地 sqlite 路径(默认生产库)")
    parser.add_argument("--apply", action="store_true", help="真正执行(默认 dry-run)")
    args = parser.parse_args()

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    factory = _make_session_factory(args.db)
    with factory() as session:
        try:
            report = split_conversation(
                session, args.chat, args.conversation, plan, apply=args.apply
            )
        except ValueError as e:
            print(f"计划校验失败: {e}")
            return 2
    mode = "已执行" if report["applied"] else "dry-run(未落库)"
    print(f"会话 #{report['conversation_id']} 拆分[{mode}]: {len(report['groups'])} 组")
    for g in report["groups"]:
        print(
            f"  {'保留' if g['conversation_id'] == report['conversation_id'] else '新建'}"
            f" #{g['conversation_id']} topic={g['topic'] or '无'} "
            f"{g['count']}条 root={g['root']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
