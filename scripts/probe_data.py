#!/usr/bin/env python3
"""数据探查 CLI:分析群聊消息的规模、时间分布、类型分布与当前分组状态。

核心逻辑已迁至 app/services/probe_service.py(ProbeService),本脚本只是命令行入口,
便于在服务器上直接运行,无需启动服务。

用法(服务器上,务必用项目 venv):
    ./.venv/bin/python scripts/probe_data.py [--chat oc_xxx] [--limit 2000]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.probe_service import ProbeService  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="群聊数据探查(只读,不改库)")
    ap.add_argument("--chat", default=None, help="只探查某群 chat_id")
    ap.add_argument("--limit", type=int, default=2000, help="最多取多少条消息分析")
    args = ap.parse_args()

    svc = ProbeService()
    report = svc.probe(chat_id=args.chat, limit=args.limit)
    print(svc.to_text(report))


if __name__ == "__main__":
    main()
