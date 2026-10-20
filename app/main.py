"""FastAPI 应用工厂与入口。

工程化要点:
- 应用通过 create_app() 工厂构建,便于测试与多实例
- lifespan 启动时初始化日志与数据库表
- 生命周期内注入 lark-oapi dispatcher 到路由层
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from functools import lru_cache

from fastapi import FastAPI

from app.core.config import get_settings
from app.core.db import SessionLocal, init_db
from app.core.logging import setup_logging
from app.services.feishu_handler import build_dispatcher

logger = logging.getLogger(__name__)


@lru_cache
def _dispatcher():
    """缓存 dispatcher(每次构建较贵)。"""
    return build_dispatcher(SessionLocal)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 应用启动
    setup_logging()
    settings = get_settings()
    app.state.dispatcher = _dispatcher()
    # 事件队列消费者(先应答后处理,见 event_queue)
    from app.services.feishu_handler import get_event_queue

    get_event_queue().start()
    # 数据表就绪
    try:
        init_db()
    except Exception:  # noqa: BLE001
        # 数据库不可用时不清零服务,交由 /ready 与日志暴露
        logger.exception("init_db 失败(数据库可能未就绪)")
    # 主题 AI 总结后台调度(建表后再启,租约锁表依赖 create_all)
    from app.services.topic_summary_service import get_topic_summary_service

    get_topic_summary_service().start()
    logger.info("服务启动完成 host=%s port=%s db=%s",
                settings.host, settings.port, settings.database_url)
    yield
    # 应用关闭(优雅):停后台调度 + 排空事件队列,处理完在途事件再退出
    get_topic_summary_service().stop()
    get_event_queue().stop(drain=True)
    logger.info("服务已关闭")


@lru_cache
def create_app() -> FastAPI:
    settings = get_settings()
    from app.api import routes as routes_module

    app = FastAPI(
        title="Feishu Group Message Capture",
        version="1.0.0",
        lifespan=lifespan,
    )

    # 把 dispatcher 挂到路由模块(工厂内提前初始化,保证路由可用)
    routes_module.dispatcher = _dispatcher()

    app.include_router(routes_module.router)
    logger.setLevel(logging.DEBUG)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    setup_logging()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        workers=settings.workers,
        log_level=settings.log_level.lower(),
    )
