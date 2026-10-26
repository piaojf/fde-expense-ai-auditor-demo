"""Create a separate, synthetic enterprise demo generation; never erase existing data."""

import argparse
import json
import os
import secrets

from apps.api.enterprise_store import EnterpriseStore
from apps.api.repository import MockClaimRepository
from domain.enterprise import CreateUser
from scripts import m6_demo as m6

ACCOUNTS = (
    ("DEMO-CN", "employee", "员工林岚", "华南销售部", "客户经理", "EMPLOYEE"),
    ("DEMO-CN", "employee.other", "员工周宁", "研发部", "工程师", "EMPLOYEE"),
    ("DEMO-CN", "finance", "财务陈敏", "财务部", "财务审核员", "FINANCE"),
    ("DEMO-CN", "admin", "管理员沈青", "企业管理部", "制度管理员", "ADMIN"),
    ("DEMO-B", "employee", "员工苏然", "销售部", "客户经理", "EMPLOYEE"),
    ("DEMO-B", "finance", "财务许禾", "财务部", "财务审核员", "FINANCE"),
    ("DEMO-B", "admin", "管理员江棠", "企业管理部", "制度管理员", "ADMIN"),
)


def init(reset_key, *, cookie_cors_consent=False):
    if not cookie_cors_consent:
        raise ValueError("Explicit human consent for localhost credentialed CORS is required")
    if not reset_key or len(reset_key) < 8:
        raise ValueError("Use a new descriptive reset key; no existing generation is overwritten")
    if m6.CURRENT.exists():
        current = json.loads(m6.CURRENT.read_text(encoding="utf-8"))
        if current.get("reset_key") == reset_key:
            directory = m6.active()
            if not (directory / "enterprise-enabled.json").exists():
                raise ValueError("Existing non-enterprise generation reserved this key")
            assert (directory / "enterprise-accounts.json").exists()
            print("Enterprise generation already initialized; credentials and data retained")
            return directory
        m6.stop()
    directory = m6.init(reset_key=reset_key)
    repo = MockClaimRepository(store_path=str(directory / "browser.sqlite3"))
    try:
        store = EnterpriseStore(repo)
        credentials = []
        for company, username, name, department, position, role in ACCOUNTS:
            password = secrets.token_urlsafe(18)
            user = store.create_user(
                company,
                CreateUser(
                    username=username,
                    password=password,
                    name=name,
                    department=department,
                    position=position,
                    role=role,
                ),
            )
            credentials.append({**user, "password": password})
        m6.private_json(directory / "enterprise-accounts.json", {"accounts": credentials})
        path = directory / "enterprise-accounts.txt"
        with path.open("w", encoding="utf-8") as target:
            os.chmod(path, 0o600)
            target.write("仅限本机合成演示。请使用个人角色账号，不共享登录会话。\n\n")
            for user in credentials:
                target.write(
                    f"企业：{user['company_id']}\n角色：{user['role']}\n"
                    f"姓名：{user['name']}\n账号：{user['username']}\n"
                    f"密码：{user['password']}\n\n"
                )
        m6.private_json(
            directory / "enterprise-enabled.json",
            {
                "profile": "employee-enterprise-v1.5",
                "cookie_cors_consent": True,
                "origin": "http://127.0.0.1:3000",
                "paid_providers": False,
            },
        )
    finally:
        repo.close()
    print("Separate enterprise generation created; previous generation preserved")
    print("Credentials are in the ignored, mode-0600 enterprise-accounts.txt")
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init", "start", "stop", "status"])
    parser.add_argument("--reset-key")
    parser.add_argument("--cookie-cors-consent", action="store_true")
    args = parser.parse_args()
    if args.action == "init":
        init(args.reset_key, cookie_cors_consent=args.cookie_cors_consent)
    elif args.action == "start":
        marker = json.loads((m6.active() / "enterprise-enabled.json").read_text(encoding="utf-8"))
        assert marker.get("cookie_cors_consent") is True
        m6.start()
    elif args.action == "stop":
        m6.stop()
    else:
        m6.status()


if __name__ == "__main__":
    main()
