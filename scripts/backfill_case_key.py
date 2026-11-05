#!/usr/bin/env python3
"""回填 messages.case_key(设备标识锚点,阶段二)。

把存量已入库消息的 case_key 补上(入库时已能自动生成,此脚本处理历史数据)。
幂等:已非空的行跳过;dry-run 只预览不写库。

用法:
    python scripts/backfill_case_key.py --dry-run            # 预览将更新条数
    python scripts/backfill_case_key.py                      # 全量回填
    python scripts/backfill_case_key.py --chat oc_xxx       # 只回填某群
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import bindparam, select, update  # noqa: E402

from app.core.db import SessionLocal, init_db  # noqa: E402
from app.models.message import Message  # noqa: E402
from app.services.case_key import extract_case_key  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="回填消息设备标识锚点 case_key")
    parser.add_argument("--chat", default="", help="只处理指定 chat_id")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写库")
    parser.add_argument("--limit", type=int, default=0, help="最多处理 N 条(0=全部)")
    args = parser.parse_args()

    init_db()

    stmt = select(Message.id, Message.text).where(Message.case_key == "")
    if args.chat:
        stmt = stmt.where(Message.chat_id == args.chat)
    if args.limit:
        stmt = stmt.limit(args.limit)

    with SessionLocal() as session:
        rows = session.execute(stmt).all()
        to_update: list[tuple[int, str]] = []
        for mid, text in rows:
            key = extract_case_key(text or "")
            if key:
                to_update.append((int(mid), key))
        print(f"扫描 {len(rows)} 条空 case_key,命中设备标识 {len(to_update)} 条")
        if args.dry_run or not to_update:
            return 0
        # 分小批提交,避免一次事务过大。
        # 用 Connection 级 execute 走原生 executemany(绕过 ORM 按主键批量更新的限制),
        # bindparam 用非保留名(b_id/b_ck),避免与列名 'id' 冲突。
        upd = (
            update(Message)
            .where(Message.id == bindparam("b_id"))
            .values(case_key=bindparam("b_ck"))
        )
        for i in range(0, len(to_update), 500):
            batch = to_update[i : i + 500]
            # 每个批次重新取连接:commit 后旧连接已失效,不可跨批复用
            conn = session.connection()
            conn.execute(
                upd,
                [{"b_id": m, "b_ck": k} for m, k in batch],
            )
            session.commit()
        print(f"已回填 {len(to_update)} 条 case_key")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
