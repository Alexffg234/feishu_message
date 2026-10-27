#!/usr/bin/env python3
"""命令行创建/管理账号(自助注册关闭时的开号入口)。

用法:
    python scripts/create_user.py admin 密码123              # 创建(首个账号自动 admin)
    python scripts/create_user.py admin 密码123 --role admin  # 指定角色
    python scripts/create_user.py admin --reset-pwd 新密码    # 重置密码
    python scripts/create_user.py admin --disable             # 停用账号
    python scripts/create_user.py --list                      # 列出全部账号
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.db import SessionLocal, init_db  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import auth_service  # noqa: E402


def _list_users(session: Session) -> None:
    rows = session.query(User).order_by(User.id).all()
    if not rows:
        print("(暂无账号,访问 /login 注册,或用本脚本创建)")
        return
    print(f"{'ID':<5}{'用户名':<20}{'角色':<10}")
    for u in rows:
        state = "" if u.is_active else "  [已停用]"
        print(f"{u.id:<5}{u.username:<20}{u.role}{state}")


def main() -> int:
    parser = argparse.ArgumentParser(description="创建 / 管理登录账号")
    parser.add_argument("username", nargs="?", help="用户名")
    parser.add_argument("password", nargs="?", help="密码")
    parser.add_argument("--role", choices=["admin", "member"], default=None)
    parser.add_argument("--display-name", default="", help="展示名")
    parser.add_argument("--reset-pwd", metavar="NEW_PASSWORD", help="重置密码")
    parser.add_argument("--disable", action="store_true", help="停用账号")
    parser.add_argument("--enable", action="store_true", help="启用账号")
    parser.add_argument("--list", action="store_true", help="列出全部账号")
    args = parser.parse_args()

    settings = get_settings()
    init_db()

    with SessionLocal() as session:
        if args.list:
            _list_users(session)
            return 0
        if not args.username:
            parser.error("缺少用户名(或用 --list)")

        user = auth_service.get_user_by_username(session, args.username)

        if args.reset_pwd:
            if user is None:
                print(f"账号不存在: {args.username}")
                return 1
            auth_service.set_password(session, user, args.reset_pwd)
            print(f"已重置密码: {user.username}")
            return 0

        if args.disable or args.enable:
            if user is None:
                print(f"账号不存在: {args.username}")
                return 1
            user.is_active = 0 if args.disable else 1
            session.commit()
            print(f"账号已{'停用' if args.disable else '启用'}: {user.username}")
            return 0

        if not args.password:
            parser.error("缺少密码")
        if user is not None:
            print(f"用户名已被占用: {args.username}")
            return 1
        try:
            created = auth_service.create_user(
                session,
                args.username,
                args.password,
                display_name=args.display_name,
                role=args.role,
            )
        except ValueError as exc:
            print(f"创建失败: {exc}")
            return 1
        print(f"已创建账号: {created.username} (role={created.role})")
        if not settings.allow_public_register:
            print("提示: 当前 ALLOW_PUBLIC_REGISTER=false,其余账号需用本脚本创建")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
