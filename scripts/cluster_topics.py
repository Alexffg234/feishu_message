#!/usr/bin/env python3
"""群聊主题归类 CLI（只读，不改库）。

把某群（或全部群）的复杂对话按主题聚类整理为「主题簇」——
解决群聊多线程交织、回复链断裂导致"同一话题被拆散"的问题。

对应设计文档 docs/TOPIC_CLUSTER_DESIGN.md；核心逻辑在 app/services/cluster_service.py。
默认【零 LLM 也能跑】（中文 2-gram + Jaccard 聚类）；服务器配了 LLM 会自动增强主题措辞。

用法（服务器上，务必用项目 venv）：
    ./.venv/bin/python scripts/cluster_topics.py                 # 全部群
    ./.venv/bin/python scripts/cluster_topics.py --chat oc_xxx   # 只看某群
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import SessionLocal  # noqa: E402
from app.dao.segment_repo import SegmentRepo  # noqa: E402
from app.services.cluster_service import cluster_chat, to_text, _load_llm  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="群聊主题归类整理（只读）")
    ap.add_argument("--chat", default=None, help="只看某群 chat_id")
    args = ap.parse_args()

    llm = _load_llm()
    if llm is not None:
        print("LLM 已启用 → 主题词由 LLM 概括、模糊边界由 LLM 判定")
    else:
        print("LLM 未配置 → 使用纯规则(高频词主题 + 2-gram 相似度聚类)")

    if args.chat:
        chats = [args.chat]
    else:
        with SessionLocal() as session:
            chats = SegmentRepo(session).list_chat_ids()

    total_topics = 0
    for cid in chats:
        print(f"\n########## 群 {cid} ##########")
        clusters = cluster_chat(cid, llm=llm)
        print(to_text(clusters))
        total_topics += len(clusters)
    print(f"\n完成: 共处理 {len(chats)} 个群, 产出 {total_topics} 个主题簇")
    return 0


if __name__ == "__main__":
    sys.exit(main())
