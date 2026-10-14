#!/usr/bin/env python3
"""离线话题段落重算脚本(方案Y:整群话题段)。

对历史消息全量重切段落:时间硬切为主,LLM 语义粗并为辅(可降级纯规则)。
幂等——重算某群前先清理该群旧段,可反复跑。

用法(服务器上,务必用项目 venv):
    ./.venv/bin/python scripts/segment_messages.py                 # 全量重算所有群
    ./.venv/bin/python scripts/segment_messages.py --chat oc_xxx   # 只重算某群
    ./.venv/bin/python scripts/segment_messages.py --dry-run       # 只预览不写库
    ./.venv/bin/python scripts/segment_messages.py --timeout 600   # 自定义时间硬切阈值(秒)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.segment_service import SegmentService  # noqa: E402


def _show(detail: dict, idx: int) -> None:
    mark = "↺" if detail.get("interleaved") else "  "
    print(
        f"  [{idx:>2}] {detail.get('repr','')}  "
        f"n={detail.get('message_count')} 人={detail.get('participant_count')} {mark}interleaved"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="离线话题段落重算(方案Y,时间硬切+LLM软切)")
    ap.add_argument("--chat", default=None, help="只重算某群 chat_id")
    ap.add_argument("--timeout", type=int, default=600, help="时间硬切阈值(秒),默认 600s=10分钟")
    ap.add_argument("--dry-run", action="store_true", help="只预览结果,不写库")
    args = ap.parse_args()

    svc = SegmentService(timeout_sec=args.timeout)

    if args.chat:
        res = svc.segment_chat(args.chat, dry_run=args.dry_run)
        results = [res]
    else:
        print("正在扫描全部群…(dry-run)" if args.dry_run else "正在全量重算所有群…")
        results = svc.segment_all(dry_run=args.dry_run)

    for res in results:
        cid = res.get("chat_id")
        if res.get("error"):
            print(f"\n=== {cid}: {res['error']} ===")
            continue
        mode = "预览" if res.get("dry_run") else "已写入"
        print(f"\n=== {cid} | 消息 {res.get('messages')} 条 -> 段落 {res.get('segments')} 个 [{mode}] ===")
        for idx, d in enumerate(res.get("segments_detail", []), start=1):
            _show(d, idx)
        if res.get("cleared_old_segments"):
            print(f"  已清理旧段 {res['cleared_old_segments']} 个(幂等)")

    print("\n完成。")


if __name__ == "__main__":
    main()
