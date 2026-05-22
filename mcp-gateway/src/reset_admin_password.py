#!/usr/bin/env python3
"""
管理员密码重置工具

Usage:
    python reset_admin_password.py <username> <new_password>
    python -m src.reset_admin_password <username> <new_password>

Example:
    python reset_admin_password.py admin NewPass123
"""
import json
import os
import sys

import bcrypt

# 默认配置文件路径（可通过环境变量覆盖）
DEFAULT_ACCOUNTS_FILE = os.path.join(
    os.path.dirname(__file__), "..", "..", "kbdata", "config", "admin_accounts.json"
)
ADMIN_ACCOUNTS_FILE = os.environ.get("ADMIN_ACCOUNTS_FILE", DEFAULT_ACCOUNTS_FILE)


def load_accounts(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_accounts(path: str, accounts: dict) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(accounts, f, ensure_ascii=False, indent=2)
        return True
    except IOError as e:
        print(f"[错误] 保存失败: {e}")
        return False


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def reset_password(username: str, new_password: str) -> bool:
    accounts = load_accounts(ADMIN_ACCOUNTS_FILE)
    account = accounts.get(username)

    if not account:
        print(f"[错误] 用户 '{username}' 不存在")
        print(f"       当前用户列表: {list(accounts.keys()) or '(空)'}")
        return False

    account["password_hash"] = hash_password(new_password)

    if save_accounts(ADMIN_ACCOUNTS_FILE, accounts):
        print(f"[成功] 用户 '{username}' 的密码已重置")
        print(f"       配置文件: {os.path.abspath(ADMIN_ACCOUNTS_FILE)}")
        return True
    return False


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        print(f"\n当前配置文件: {os.path.abspath(ADMIN_ACCOUNTS_FILE)}")
        sys.exit(1)

    username = sys.argv[1]
    new_password = sys.argv[2]

    if len(new_password) < 6:
        print("[错误] 新密码长度至少 6 位")
        sys.exit(1)

    success = reset_password(username, new_password)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
