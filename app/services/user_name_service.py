"""用户真实姓名惰性反查服务。

飞书回调里消息 sender_id 存的是 open_id(如 ou_xxx),不是真实姓名。
本服务把 open_id 反查成真实姓名(name),并缓存到 user_names 表:
- 首次遇到某发送者时调飞书通讯录接口(contact/v3/user.get,user_id_type=open_id)拿姓名,写入缓存
- 之后直接从缓存读,避免频繁调飞书
- 反查失败/未授权时标记 failed=1,不再重复打飞书(优雅降级:返回 open_id 原值)

需要飞书应用权限: contact:user.base:readonly(通讯录只读)。
无凭据/无权限/调用失败 都不影响主流程,只回退显示 open_id。
"""
from __future__ import annotations

import logging
from functools import lru_cache

import lark_oapi as lark
from lark_oapi.api.contact.v3 import GetUserRequest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.user_name import UserName

logger = logging.getLogger(__name__)

# 反查的 ID 类型:我们存的是 open_id
_USER_ID_TYPE = "open_id"


class UserNameService:
    def __init__(self, client: lark.Client | None = None):
        self._client = client

    # ------------------------------------------------------------ 公开
    def resolve(self, user_id: str) -> str:
        """把 open_id 解析成真实姓名;无法解析时返回原 open_id。"""
        user_id = user_id or ""
        if not user_id:
            return ""
        name = self._cache_get(user_id)
        if name is not None:  # 已缓存(含成功名或 failed 标记)
            return name
        # 未缓存 → 调飞书反查并写入缓存
        name = self._resolve_and_cache(user_id)
        return name or user_id

    def resolve_many(self, user_ids: list[str]) -> dict[str, str]:
        """批量把 open_id 解析成真实姓名,返回 {open_id: 姓名}。缺失/失败回退 open_id。"""
        result: dict[str, str] = {}
        to_fetch: list[str] = []
        for uid in user_ids or []:
            if not uid:
                continue
            name = self._cache_get(uid)
            if name is not None:
                result[uid] = name
            else:
                to_fetch.append(uid)
        for uid in to_fetch:
            result[uid] = self._resolve_and_cache(uid) or uid
        return result

    # ------------------------------------------------------------ 缓存
    def _cache_get(self, user_id: str) -> str | None:
        """查缓存。返回 None 表示未缓存;返回 '' 表示已缓存但 failed(仍视为'有缓存')。"""
        try:
            with SessionLocal() as session:
                row = session.execute(
                    select(UserName).where(UserName.user_id == user_id)
                ).scalar_one_or_none()
                if row is None:
                    return None
                if row.failed:
                    return ""  # 之前失败过,不再重复打飞书
                return row.name
        except Exception as e:  # noqa: BLE001 - 表未建/连接异常统一降级为未缓存
            logger.warning("读取用户姓名缓存失败 %s: %s", user_id, e)
            return None

    def _cache_put(self, user_id: str, name: str, failed: int) -> None:
        try:
            with SessionLocal() as session:
                row = session.execute(
                    select(UserName).where(UserName.user_id == user_id)
                ).scalar_one_or_none()
                if row is None:
                    row = UserName(user_id=user_id, name=name, failed=failed)
                    session.add(row)
                else:
                    row.name = name
                    row.failed = failed
                session.commit()
        except Exception as e:  # noqa: BLE001 - 表未建/连接异常,忽略写入(下次再试)
            logger.warning("写入用户姓名缓存失败 %s: %s", user_id, e)

    # ------------------------------------------------------------ 飞书反查
    def _get_client(self) -> lark.Client | None:
        if self._client is not None:
            return self._client
        settings = get_settings()
        if not (settings.app_id and settings.app_secret):
            return None
        return (
            lark.Client.builder()
            .app_id(settings.app_id)
            .app_secret(settings.app_secret)
            .build()
        )

    def _fetch_name(self, client: lark.Client, user_id: str) -> tuple[str, int]:
        """调飞书按 open_id 查姓名。返回 (name, failed)。失败时 name='' failed=1。"""
        try:
            req = (
                GetUserRequest.builder()
                .user_id_type(_USER_ID_TYPE)
                .user_id(user_id)
                .build()
            )
            resp = client.contact.v3.user.get(req)
            if resp is None or not resp.success():
                logger.warning("反查用户姓名失败 %s: code=%s msg=%s",
                               user_id,
                               getattr(resp, "code", "?"),
                               getattr(resp, "msg", "?"))
                return "", 1
            user = resp.data.user if (resp.data is not None) else None
            name = (user.name if user and getattr(user, "name", None) else "") or ""
            if name:
                return name, 0
            # 成功但无名(可能被@的是机器人/特殊主体)
            return "", 1
        except Exception as e:  # noqa: BLE001 - 网络/权限等异常统一降级
            logger.warning("反查用户姓名异常 %s: %s", user_id, e)
            return "", 1

    def _resolve_and_cache(self, user_id: str) -> str:
        client = self._get_client()
        if client is None:
            # 无应用凭据(未配置),直接标记 failed 避免每次重复检查
            self._cache_put(user_id, "", 1)
            return ""
        name, failed = self._fetch_name(client, user_id)
        self._cache_put(user_id, name, failed)
        return name


@lru_cache
def get_user_name_service() -> UserNameService:
    """应用级单例。"""
    return UserNameService()


def fill_tree_sender_names(tree_roots: list, service: UserNameService | None = None) -> None:
    """就地给对话树节点补充 sender_name(真实姓名)。

    tree_roots 是 list_tree 返回的 roots(每个节点 {message:{sender_id,...}, children:[...]})。
    用 service 批量把 open_id 解析成姓名,写入 message["sender_name"]。解析失败保持原 open_id。
    """
    if not tree_roots:
        return
    svc = service or get_user_name_service()

    # 先收集所有出现的 sender_id(去重)
    seen: set[str] = set()

    def collect(nodes):
        for n in nodes:
            m = n.get("message") or {}
            sid = (m.get("sender_id") or "") if isinstance(m, dict) else ""
            if sid:
                seen.add(sid)
            collect(n.get("children", []))

    collect(tree_roots)
    name_map = svc.resolve_many(list(seen))

    def apply(nodes):
        for n in nodes:
            m = n.get("message")
            if isinstance(m, dict) and m.get("sender_id"):
                m["sender_name"] = name_map.get(m["sender_id"], m["sender_id"])
            apply(n.get("children", []))

    apply(tree_roots)
