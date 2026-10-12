"""统一日志配置。

支持控制台输出 + 滚动文件日志,生产环境便于采集与排查。
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.core.config import get_settings

_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def setup_logging(level: str | None = None, log_file: str | None = None) -> None:
    """初始化根日志。幂等,可重复调用。"""
    settings = get_settings()
    level = (level or settings.log_level or "INFO").upper()
    log_file = log_file if log_file is not None else settings.log_file

    root = logging.getLogger()
    root.setLevel(level)

    # 避免重复添加 handler
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = logging.Formatter(_FORMAT)

    # 控制台
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    # 滚动文件(可选)
    if log_file:
        path = Path(log_file)
        # 防御:若 path 指向的是已存在目录(而非文件),跳过文件日志而非崩溃
        if path.exists() and path.is_dir():
            print(f"[logging] 警告: LOG_FILE 指向了目录而非文件,已跳过文件日志: {log_file}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)

    # 降噪:降低第三方库的冗余日志
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("lark_oapi").setLevel(logging.WARNING)
