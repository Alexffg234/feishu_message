"""数据库核心:SQLAlchemy engine / Session 管理 / 建表。

支持 SQLite(开发)、PostgreSQL 与 MySQL(生产)。生产建议用连接池参数。
"""
from __future__ import annotations

import logging
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def _build_engine_url() -> str:
    url = get_settings().database_url
    # 确保 sqlite 的父目录存在
    if url.startswith("sqlite:///"):
        db_path = url.replace("sqlite:///", "", 1)
        p = Path(db_path)
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
    # PyMySQL 支持 mysql:// 与 mysql+pymysql:// 两种前缀,统一规范化
    if url.startswith("mysql://"):
        url = url.replace("mysql://", "mysql+pymysql://", 1)
    return url


def create_engine_sync() -> Engine:
    """创建同步 engine。SQLite 关闭 check_same_thread;PG/MySQL 用连接池。"""
    url = _build_engine_url()
    is_server_db = url.startswith("postgresql") or url.startswith("mysql")
    kwargs: dict = {"pool_pre_ping": True}
    if is_server_db:
        kwargs.update(
            pool_size=5,
            max_overflow=10,
            pool_recycle=1800,
        )
    else:
        kwargs.update(connect_args={"check_same_thread": False})
    return create_engine(url, **kwargs)


engine = create_engine_sync()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_session() -> Generator[Session, None, None]:
    """FastAPI 依赖:每个请求一个 Session,用完即关。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """建表(若不存在)并为存量表补齐回复分组字段。生产可用 Alembic 迁移替代。"""
    from app.models import Base  # 导入包即注册全部模型(含 conversations/message_links)

    Base.metadata.create_all(bind=engine)
    _ensure_message_group_columns()
    _ensure_conversation_columns()
    _ensure_segment_columns()
    logger.info("数据库表结构就绪: %s", get_settings().database_url)


def _ensure_message_group_columns() -> None:
    """轻量迁移:为存量 messages 表补 parent_message_id / root_message_id。

    - 缺列则 ALTER TABLE ADD COLUMN(SQLite/MySQL/PostgreSQL 均支持)
    - 回填:老消息无回复信息,每条自成一个组(root=自身)
    """
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text as sql_text

    inspector = sa_inspect(engine)
    if "messages" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("messages")}

    with engine.begin() as conn:
        for col in ("parent_message_id", "root_message_id", "topic"):
            if col not in existing:
                conn.execute(
                    sql_text(
                        f"ALTER TABLE messages ADD COLUMN {col} "
                        "VARCHAR(128) NOT NULL DEFAULT ''"
                    )
                )
                logger.info("messages 表新增列 %s", col)
        conn.execute(
            sql_text(
                "UPDATE messages SET root_message_id = message_id "
                "WHERE root_message_id = ''"
            )
        )


def _ensure_conversation_columns() -> None:
    """轻量迁移:为存量 messages 表补 conversation_id 列(FK→conversations.id)。

    conversation_id 默认 0 = 尚未挂到对话,由回填脚本(backfill_conversations.py)
    统一把存量 parent/root 关系落成 conversations + message_links 两条新表。
    """
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text as sql_text

    inspector = sa_inspect(engine)
    if "messages" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("messages")}
    if "conversation_id" in existing:
        return
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "ALTER TABLE messages ADD COLUMN conversation_id "
                "BIGINT NOT NULL DEFAULT 0"
            )
        )
        logger.info("messages 表新增列 conversation_id")


def _ensure_segment_columns() -> None:
    """轻量迁移:为存量 messages 表补 segment_id 列(FK→message_segments.id)。

    segment_id 默认 0 = 尚未归段,由离线重算脚本(scripts/segment_messages.py)
    统一切段并回填。新建的 messages 表上 create_all 已含该列,这里只处理存量。
    """
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text as sql_text

    inspector = sa_inspect(engine)
    if "messages" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("messages")}
    if "segment_id" in existing:
        return
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "ALTER TABLE messages ADD COLUMN segment_id "
                "BIGINT NOT NULL DEFAULT 0"
            )
        )
        logger.info("messages 表新增列 segment_id")


def check_db_connection() -> bool:
    """健康检查用:尝试执行一条轻量查询。"""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        logger.exception("数据库连接检查失败")
        return False
