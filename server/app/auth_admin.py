from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from .auth import (
    generate_initial_password,
    initial_credentials_directory,
    reset_local_password,
    write_local_credentials,
)
from .config import Settings
from .database import Database
from .schema_migrations import migrate_database


def _password_from_prompt() -> str:
    first = getpass.getpass("请输入新密码（至少12个字符）：")
    second = getpass.getpass("请再次输入新密码：")
    if first != second:
        raise ValueError("两次输入的密码不一致")
    return first


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="显式重置意向宝本地账号密码；不会改变角色或启用状态。"
    )
    parser.add_argument("username", choices=("admin", "planner"))
    parser.add_argument("--data-dir", type=Path)
    password_source = parser.add_mutually_exclusive_group()
    password_source.add_argument(
        "--password-env",
        help="自动化场景可从指定环境变量读取新密码；默认使用隐藏输入。",
    )
    password_source.add_argument(
        "--generate",
        action="store_true",
        help="生成强随机密码并写入受控本地凭据文件；终端不显示密码。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.generate:
            password = generate_initial_password()
        elif args.password_env:
            password = os.getenv(args.password_env)
            if not password:
                raise ValueError(f"环境变量未设置：{args.password_env}")
        else:
            password = _password_from_prompt()

        settings = Settings.load(data_dir=args.data_dir)
        migrate_database(settings.server_dir, settings.database_url)
        database = Database(settings.database_url)
        database.create_all()
        generated_file: Path | None = None
        try:
            with database.session() as session:
                reset_local_password(
                    session,
                    username=args.username,
                    new_password=password,
                    commit=not args.generate,
                )
                if args.generate:
                    try:
                        credentials_dir = write_local_credentials(
                            settings.data_dir,
                            {args.username: password},
                        )
                        if credentials_dir is None:
                            raise OSError("未能创建本地凭据目录")
                        generated_file = credentials_dir / f"{args.username}.txt"
                        session.commit()
                    except Exception:
                        session.rollback()
                        if generated_file is not None:
                            generated_file.unlink(missing_ok=True)
                        raise
        finally:
            database.engine.dispose()

        if args.generate:
            print(
                f"账号 {args.username} 的随机强密码已写入：{generated_file}",
                flush=True,
            )
            print("终端未显示密码；已有登录会话已失效。", flush=True)
        else:
            stale_initial_file = (
                initial_credentials_directory(settings.data_dir)
                / f"{args.username}.txt"
            )
            stale_initial_file.unlink(missing_ok=True)
            print(
                f"账号 {args.username} 的密码已重置，已有登录会话已失效。",
                flush=True,
            )
        return 0
    except (OSError, ValueError) as exc:
        print(f"密码重置失败：{exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
