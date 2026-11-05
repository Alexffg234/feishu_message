#!/usr/bin/env python3
"""命令行维护标签别名(topic_aliases:alias -> canonical 收敛表)。

阶段一(话题归纳)的离线管理入口:同义词标签靠别名表收敛后归一写入/归并,
本脚本供生产上不依赖 Web 界面直接增删改查别名。

用法:
    python scripts/add_alias.py --add "投不了屏" "投屏"       # 新增/更新别名
    python scripts/add_alias.py --list                       # 列出全部别名
    python scripts/add_alias.py --remove "投不了屏"           # 删除别名
    python scripts/add_alias.py --import std                 # 无操作示例占位(预留)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import SessionLocal, init_db  # noqa: E402
from app.models.topic_alias import TopicAlias  # noqa: E402
from app.services.label_norm import clean_label  # noqa: E402


def _list_aliases(session) -> None:
    rows = session.query(TopicAlias).order_by(TopicAlias.canonical, TopicAlias.alias).all()
    if not rows:
        print("(暂无别名,用 --add 添加)")
        return
    print(f"{'别名':<20}{'标准标签':<20}{'来源':<8}")
    for r in rows:
        src = "自动" if r.auto else "人工"
        print(f"{r.alias:<20}{r.canonical:<20}{src}")
    print(f"\n共 {len(rows)} 条别名")


def main() -> int:
    parser = argparse.ArgumentParser(description="维护标签别名表")
    parser.add_argument("--add", nargs=2, metavar=("ALIAS", "CANONICAL"), help="新增/更新别名")
    parser.add_argument("--remove", metavar="ALIAS", help="删除别名")
    parser.add_argument("--list", action="store_true", help="列出全部别名")
    args = parser.parse_args()

    init_db()

    with SessionLocal() as session:
        if args.list:
            _list_aliases(session)
            return 0
        if args.add:
            alias = clean_label(args.add[0])
            canonical = clean_label(args.add[1])
            if not alias or not canonical:
                print("别名与标准标签均不能为空(语气词无效)")
                return 1
            if alias == canonical:
                print("别名与标准标签不能相同")
                return 1
            row = session.query(TopicAlias).filter(TopicAlias.alias == alias).first()
            if row is None:
                session.add(TopicAlias(alias=alias, canonical=canonical, auto=0))
                session.commit()
                print(f"已新增别名: {alias} -> {canonical}")
            else:
                row.canonical = canonical
                row.auto = 0
                session.commit()
                print(f"已更新别名: {alias} -> {canonical}")
            return 0
        if args.remove:
            row = session.query(TopicAlias).filter(TopicAlias.alias == args.remove).first()
            if row is None:
                print(f"别名不存在: {args.remove}")
                return 1
            session.delete(row)
            session.commit()
            print(f"已删除别名: {args.remove}")
            return 0
        parser.error("缺少操作(--add / --remove / --list)")


if __name__ == "__main__":
    raise SystemExit(main())
