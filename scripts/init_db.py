#!/usr/bin/env python3
"""初始化数据库表结构。

用法:
    python scripts/init_db.py
生产环境首次部署前执行;或借助 docker-compose 的健康检查自动完成。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 让脚本可从项目根直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings  # noqa: E402
from app.core.db import init_db  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402


def main() -> None:
    setup_logging()
    settings = get_settings()
    print(f"初始化数据库: {settings.database_url}")
    init_db()
    print("完成: 数据表已就绪")


if __name__ == "__main__":
    main()
