"""回填发送者真实姓名到 user_names 缓存表。

两条取名字的路子,按顺序跑:
1) 通讯录反查(contact.v3.user.get):需要 contact:user.base:readonly + 用户在应用可用范围内。
   不在可用范围内(含外部群成员)会报 41050。
2) 从 @提及里捞:调「获取消息」接口(im/v1/messages/{message_id})拿原始 mentions,
   里面带 {id(open_id), name}。只要某人被别人 @ 过一次就能拿到姓名,
   不依赖通讯录权限,也不受可用范围限制。只需 im:message:readonly。

用途:
- 飞书权限/可用范围调整后,之前反查失败的 open_id 会被缓存成 failed=1 且不再重试;
  本脚本先清掉这些失败缓存,再重新回填。

用法(服务器用 venv):
    ./.venv/bin/python scripts/backfill_user_names.py
    ./.venv/bin/python scripts/backfill_user_names.py --skip-mentions   # 只走通讯录反查
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.services.user_name_service import get_user_name_service  # noqa: E402, F401

_MSG_URL = "https://open.feishu.cn/open-apis/im/v1/messages/{message_id}"


def _tenant_token() -> str:
    settings = get_settings()
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": settings.app_id, "app_secret": settings.app_secret},
        timeout=10,
    ).json()
    return resp.get("tenant_access_token", "")


def harvest_from_mentions(session, token: str, known: set[str]) -> dict[str, str]:
    """扫描带 @提及的历史消息,从原始 mentions 里捞 {open_id: 姓名}。

    known: 已经拿到姓名的 open_id,这些不用管。
    """
    rows = session.execute(
        text("SELECT message_id FROM messages WHERE raw LIKE '%mentions%'")
    ).fetchall()
    message_ids = [r[0] for r in rows if r[0]]
    print(f"扫描含 @提及的消息: {len(message_ids)} 条")

    headers = {"Authorization": "Bearer " + token}
    found: dict[str, str] = {}
    for idx, mid in enumerate(message_ids, 1):
        try:
            resp = requests.get(_MSG_URL.format(message_id=mid), headers=headers, timeout=10).json()
        except Exception as e:  # noqa: BLE001 - 单条失败不影响整体
            print("  拉取消息失败", mid, e)
            continue
        if resp.get("code") != 0:
            continue
        for item in resp.get("data", {}).get("items") or []:
            for m in item.get("mentions") or []:
                uid = (m.get("id") or "").strip()
                name = (m.get("name") or "").strip()
                if uid and name and uid not in known:
                    found.setdefault(uid, name)
        if idx % 50 == 0:
            print(f"  已扫描 {idx}/{len(message_ids)},累计捞到 {len(found)} 个姓名")
        time.sleep(0.05)

    # 写入缓存
    for uid, name in found.items():
        session.execute(
            text(
                "INSERT INTO user_names (user_id, name, failed) VALUES (:u, :n, 0) "
                "ON DUPLICATE KEY UPDATE name = :n, failed = 0"
            ),
            {"u": uid, "n": name},
        )
    if found:
        session.commit()
    return found


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-mentions", action="store_true", help="跳过从 @提及里捞姓名")
    args = parser.parse_args()

    with SessionLocal() as session:
        cleared = session.execute(
            text("DELETE FROM user_names WHERE failed = 1 OR name IS NULL OR name = ''")
        ).rowcount
        session.commit()
        print(f"清理失败/空缓存: {cleared} 行")

        rows = session.execute(
            text("SELECT DISTINCT sender_id FROM messages WHERE sender_id IS NOT NULL AND sender_id <> ''")
        ).fetchall()
        ids = [r[0] for r in rows if r[0]]

    print(f"待解析发送者: {len(ids)} 个")
    if not ids:
        return 0

    svc = get_user_name_service()
    name_map = svc.resolve_many(ids)

    resolved = {k: v for k, v in name_map.items() if v and not v.startswith("ou_")}
    print(f"通讯录反查成功: {len(resolved)} / {len(name_map)}")

    mention_names: dict[str, str] = {}
    if not args.skip_mentions:
        token = _tenant_token()
        if not token:
            print("获取 tenant_access_token 失败,跳过 @提及回填")
        else:
            with SessionLocal() as session:
                mention_names = harvest_from_mentions(session, token, set(resolved))
            print(f"从 @提及补到: {len(mention_names)} 个")
            name_map.update(mention_names)

    ok = sorted({v for v in name_map.values() if v and not v.startswith("ou_")})
    failed = [k for k, v in name_map.items() if not v or v.startswith("ou_")]

    print(f"最终成功解析: {len(name_map) - len(failed)} / {len(name_map)}")
    print("姓名列表:", "、".join(ok) or "(空)")
    if failed:
        print(f"仍未解析(没被 @ 过且不在可用范围): {len(failed)} 个")
        for uid in failed:
            print("  X", uid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
