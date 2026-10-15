#!/usr/bin/env python3
"""会话缝合脚本:LLM 判定问题处理生命周期(提问→排查→对话→解决),合并同群相邻会话。

与 rethread_backfill 的区别:不依赖 LLM 标签,直接看代表消息内容,
解决「无回复链 + 标签缺失/垃圾标签(如"好的")」导致的同一问题被拆散。

默认 dry-run 只预览不写库;--apply 才真正执行合并。
--db 指向本地 sqlite 文件(配合 export_chat.py 做本地验证,不碰真实库)。

用法:
    python scripts/stitch_conversations.py --chat oc_xxx --dry-preview  # 预览
    python scripts/stitch_conversations.py --chat oc_xxx --apply        # 执行
    python scripts/stitch_conversations.py --chat oc_xxx --db data/local.sqlite --apply
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.models.conversation import Conversation  # noqa: E402
from app.services.conv_stitch import plan_stitches, stitch_chat  # noqa: E402


def _make_session_factory(db_path: str | None):
    if db_path:
        engine = create_engine(f"sqlite:///{db_path}")
        return sessionmaker(bind=engine)
    return SessionLocal


def _fmt_ms(ms: int) -> str:
    from datetime import datetime

    try:
        return datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "-"


def _conv_brief(session, conv_id: int) -> str:
    c = session.get(Conversation, conv_id)
    if c is None:
        return f"#{conv_id}(已不存在)"
    return (
        f"#{conv_id} [{_fmt_ms(c.started_at_ms)}~{_fmt_ms(c.last_message_at_ms)}] "
        f"{c.message_count}条 topic={c.topic or '无'}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="会话缝合(LLM 生命周期判定)")
    parser.add_argument("--chat", required=True, help="群 chat_id")
    parser.add_argument("--db", default=None, help="本地 sqlite 路径(本地验证用)")
    parser.add_argument("--apply", action="store_true", help="真正执行合并(默认只预览)")
    args = parser.parse_args()

    setup_logging()
    settings = get_settings()
    if not settings.llm_enabled:
        logger.error("LLM 未配置(需设置 LLM_API_KEY / LLM_MODEL)")
        return 1
    from app.services.llm import LLMClient

    llm = LLMClient(
        settings.llm_api_base,
        settings.llm_api_key,
        settings.llm_model,
        settings.llm_timeout,
    )
    factory = _make_session_factory(args.db)

    with factory() as session:
        pairs = plan_stitches(session, args.chat, llm)

    print(f"群 {args.chat} 预览: 拟合并 {len(pairs)} 对")
    for src_id, tgt_id in pairs:
        with factory() as session:
            print(f"  源 {_conv_brief(session, src_id)}")
            print(f"  -> 目标 {_conv_brief(session, tgt_id)}")

    if not args.apply:
        print("\n(dry-run 预览,未改库;加 --apply 执行合并)")
        return 0

    with factory() as session:
        done = stitch_chat(session, args.chat, llm)
    print(f"\n已合并 {len(done)} 对:")
    for src_id, tgt_id in done:
        with factory() as session:
            print(f"  {_conv_brief(session, tgt_id)} <- #{src_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
