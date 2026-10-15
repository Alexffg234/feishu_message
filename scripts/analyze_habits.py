#!/usr/bin/env python3
"""群聊对话习惯分析 CLI（只读，不改库）。

核心逻辑已迁至 app/services/habits_service.py（HabitsService.analyze），
本脚本只是命令行入口，便于在服务器上直接运行，无需启动服务。

用法（服务器上，务必用项目 venv）：
    ./.venv/bin/python scripts/analyze_habits.py
    ./.venv/bin/python scripts/analyze_habits.py --chat oc_xxx
    ./.venv/bin/python scripts/analyze_habits.py --limit 2000
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.habits_service import analyze, to_text  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="群聊对话习惯分析（只读，不改库）")
    ap.add_argument("--chat", default=None, help="只看某群 chat_id")
    ap.add_argument("--limit", type=int, default=2000, help="最多取多少条消息（默认2000）")
    args = ap.parse_args()

    report = analyze(chat_id=args.chat, limit=args.limit)
    print(to_text(report))


if __name__ == "__main__":
    main()
