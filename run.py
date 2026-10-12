#!/usr/bin/env python3
"""本地开发启动入口。

用法:
    python run.py                # 默认配置启动
    python run.py --reload       # 热重载
"""
from __future__ import annotations

import argparse

from app.core.config import get_settings
from app.core.logging import setup_logging


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reload", action="store_true", help="开发热重载")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    settings = get_settings()
    host = args.host or settings.host
    port = args.port or settings.port

    setup_logging()
    print("=" * 64)
    print("飞书群聊消息采集服务")
    print(f"  监听:    http://{host}:{port}")
    print(f"  回调:    http://<公网地址>{settings.event_path}")
    print(f"  数据库:  {settings.database_url}")
    print(f"  配置就绪: {settings.is_ready}")
    if not settings.is_ready:
        print("  ⚠ 请先在 config/.env 填写 APP_ID / APP_SECRET / VERIFICATION_TOKEN")
    print("=" * 64)

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=args.reload,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
