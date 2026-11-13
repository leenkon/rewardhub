import base64
import contextlib
import io
import os
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path
from xml.etree import ElementTree

BOOTSTRAP_DIR = None


def setUpModule():
    """在 import app 之前，先把模块级的库指向一个临时目录。"""
    global BOOTSTRAP_DIR
    BOOTSTRAP_DIR = tempfile.TemporaryDirectory()
    os.environ["DATA_DIR"] = BOOTSTRAP_DIR.name
    os.environ["DATABASE_PATH"] = str(Path(BOOTSTRAP_DIR.name) / "bootstrap.db")
    import app  # noqa: F401


def tearDownModule():
    BOOTSTRAP_DIR.cleanup()


class PointsManagerApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.temp_dir.name
        os.environ["DATABASE_PATH"] = str(Path(cls.temp_dir.name) / "test.db")
        os.environ["ADMIN_USERNAME"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "admin123"
        from app import (
            admin_credentials_from_env,
            admin_credentials_marker_path,
            app,
            connection,
            create_admin_account,
            init_db,
            mark_install_credentials_applied,
            password_hash,
        )
        import app as app_module

        app_module.DATA_DIR = Path(cls.temp_dir.name)
        app_module.DATABASE_PATH = Path(cls.temp_dir.name) / "test.db"
        init_db()
        cls.app_module = app_module
        cls.app = app
        cls.client = app.test_client()
        cls.connection = connection
        cls.create_admin_account = create_admin_account
        cls.init_db = init_db
        cls.admin_credentials_marker_path = admin_credentials_marker_path
        cls.mark_install_credentials_applied = mark_install_credentials_applied
        cls.password_hash = password_hash
        cls.admin_credentials_from_env = admin_credentials_from_env

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def setUp(self):
        self.client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        accounts = self.client.get("/api/accounts").get_json()["accounts"]
        for account in accounts:
            if account["role"] == "child" or account["username"] != "admin":
                self.client.delete(f"/api/accounts/{account['id']}")
        with type(self).connection() as conn:
            conn.execute("UPDATE app_settings SET value = '100' WHERE key = 'points_per_yuan'")
            conn.execute("DELETE FROM announcements")
            conn.execute("DELETE FROM item_bindings")
            conn.execute("DELETE FROM item_rule_overrides")
            for table in ("earn_items", "deduct_items", "rewards"):
                conn.execute(f"DELETE FROM {table}")

    def create_child(self, username="child1", display_name="小明", password="child123"):
        response = self.client.post(
            "/api/accounts",
            json={"username": username, "display_name": display_name, "password": password, "role": "child"},
        )
        self.assertEqual(response.status_code, 201)
        return response.get_json()

    def state(self):
        response = self.client.get("/api/state")
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def create_item(self, kind, name, points):
        response = self.client.post(f"/api/items/{kind}", json={"name": name, "points": points})
        self.assertEqual(response.status_code, 201)
        return response.get_json()["id"]

    def test_starts_with_admin_only(self):
        response = self.client.get("/api/accounts")
        self.assertEqual(response.status_code, 200)
        accounts = response.get_json()["accounts"]
        self.assertEqual({account["username"] for account in accounts}, {"admin"})

        data = self.state()
        self.assertIsNone(data["active_child"])
        self.assertEqual(data["total_points"], 0)
        self.assertEqual(data["earn_items"], [])
        self.assertEqual(data["deduct_items"], [])
        self.assertEqual(data["rewards"], [])
        self.assertEqual(len(data["account_overview"]), 1)

    def test_login_roles_and_child_isolation(self):
        self.create_child(username="child", display_name="小朋友")
        earn_id = self.create_item("earn", "完成作业", 10)
        response = self.client.post("/api/transactions", json={"kind": "earn", "item_id": earn_id})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["total_points"], 10)

        response = self.client.post(
            "/api/accounts",
            json={"username": "kid2", "display_name": "小明", "password": "kid12345", "role": "child"},
        )
        self.assertEqual(response.status_code, 201)
        kid2_id = response.get_json()["id"]
        response = self.client.post(
            "/api/accounts",
            json={"username": "manager2", "display_name": "家长二", "password": "manager123", "role": "admin"},
        )
        self.assertEqual(response.status_code, 201)
        actions = {log["action"] for log in self.state()["account_logs"]}
        self.assertIn("create_child", actions)
        self.assertIn("create_admin", actions)

        response = self.client.post("/api/auth/select-child", json={"child_id": kid2_id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["total_points"], 0)

        response = self.client.post("/api/transactions", json={"kind": "earn", "points": 6, "name": "测试"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["total_points"], 6)

        self.client.post("/api/auth/logout")
        response = self.client.post("/api/auth/login", json={"username": "child", "password": "child123"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["total_points"], 10)
        self.assertEqual(self.client.post("/api/system/reset").status_code, 403)

    def test_first_run_admin_setup(self):
        with type(self).connection() as conn:
            for table in ("account_logs", "point_requests", "records", "task_assignments", "account_achievements", "tasks", "earn_items", "deduct_items", "rewards", "accounts"):
                conn.execute(f"DELETE FROM {table}")

        try:
            self.client.post("/api/auth/logout")
            self.assertEqual(self.client.get("/api/setup/status").get_json(), {"configured": False})
            self.assertEqual(self.client.get("/api/state").status_code, 401)

            response = self.client.post(
                "/api/setup/admin",
                json={"username": "my-admin", "password": "secure123", "password_confirm": "secure123"},
            )
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.get_json()["user"]["username"], "my-admin")
            self.assertEqual(self.client.get("/api/setup/status").get_json(), {"configured": True})
            self.assertEqual(
                self.client.post(
                    "/api/setup/admin",
                    json={"username": "another-admin", "password": "secure123", "password_confirm": "secure123"},
                ).status_code,
                409,
            )
            self.client.post("/api/auth/logout")
            self.assertEqual(
                self.client.post("/api/auth/login", json={"username": "my-admin", "password": "secure123"}).status_code,
                200,
            )
        finally:
            with type(self).connection() as conn:
                for table in ("account_logs", "point_requests", "records", "earn_items", "deduct_items", "rewards", "accounts"):
                    conn.execute(f"DELETE FROM {table}")
                type(self).create_admin_account(conn, "admin", "admin123")

    def test_child_can_register_from_auth_flow(self):
        self.create_item("earn", "完成作业", 10)
        self.client.post("/api/auth/logout")
        response = self.client.post(
            "/api/auth/register-child",
            json={
                "username": "self-child",
                "display_name": "自助孩子",
                "password": "child123",
                "password_confirm": "child123",
                "avatar": "girl",
                "role": "admin",
            },
        )
        self.assertEqual(response.status_code, 201)
        data = response.get_json()
        self.assertEqual(data["user"]["role"], "child")
        self.assertEqual(data["user"]["avatar"], "girl")
        self.assertEqual(data["active_child"]["username"], "self-child")
        self.assertTrue(data["earn_items"])

        self.client.post("/api/auth/logout")
        self.assertEqual(
            self.client.post(
                "/api/auth/login", json={"username": "self-child", "password": "child123"}
            ).status_code,
            200,
        )
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        self.assertIn("register_child", {log["action"] for log in self.state()["account_logs"]})

    def test_child_registration_requires_admin_setup(self):
        with type(self).connection() as conn:
            conn.execute("DELETE FROM accounts")
        try:
            response = self.client.post(
                "/api/auth/register-child",
                json={
                    "username": "orphan-child",
                    "display_name": "孤立孩子",
                    "password": "child123",
                    "password_confirm": "child123",
                    "avatar": "boy",
                },
            )
            self.assertEqual(response.status_code, 409)
        finally:
            with type(self).connection() as conn:
                type(self).create_admin_account(conn, "admin", "admin123")

    def test_install_credentials_replace_untouched_legacy_admin(self):
        os.environ["ADMIN_USERNAME"] = "new-admin"
        os.environ["ADMIN_PASSWORD"] = "new-secret"
        try:
            type(self).init_db()
            self.client.post("/api/auth/logout")
            self.assertEqual(
                self.client.post("/api/auth/login", json={"username": "new-admin", "password": "new-secret"}).status_code,
                200,
            )
            self.client.post("/api/auth/logout")
            self.assertEqual(
                self.client.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).status_code,
                401,
            )
        finally:
            os.environ["ADMIN_USERNAME"] = "admin"
            os.environ["ADMIN_PASSWORD"] = "admin123"
            with type(self).connection() as conn:
                conn.execute(
                    "UPDATE accounts SET username = ?, password_hash = ?, display_name = ? WHERE role = 'admin'",
                    ("admin", type(self).password_hash("admin123"), "管理员"),
                )
            type(self).mark_install_credentials_applied(("admin", "admin123"))

    def test_install_credentials_replace_existing_custom_admin_once(self):
        os.environ["ADMIN_USERNAME"] = "wizard-admin"
        os.environ["ADMIN_PASSWORD"] = "wizard-secret"
        marker = type(self).admin_credentials_marker_path()
        marker.unlink(missing_ok=True)
        try:
            with type(self).connection() as conn:
                conn.execute(
                    "UPDATE accounts SET username = ?, password_hash = ? WHERE role = 'admin'",
                    ("old-admin", type(self).password_hash("old-secret")),
                )
            type(self).init_db()
            self.client.post("/api/auth/logout")
            self.assertEqual(
                self.client.post(
                    "/api/auth/login", json={"username": "wizard-admin", "password": "wizard-secret"}
                ).status_code,
                200,
            )
            self.assertEqual(
                self.client.post(
                    "/api/auth/login", json={"username": "old-admin", "password": "old-secret"}
                ).status_code,
                401,
            )

            with type(self).connection() as conn:
                conn.execute(
                    "UPDATE accounts SET password_hash = ? WHERE username = ?",
                    (type(self).password_hash("manual-secret"), "wizard-admin"),
                )
            type(self).init_db()
            self.assertEqual(
                self.client.post(
                    "/api/auth/login", json={"username": "wizard-admin", "password": "manual-secret"}
                ).status_code,
                200,
            )
        finally:
            os.environ["ADMIN_USERNAME"] = "admin"
            os.environ["ADMIN_PASSWORD"] = "admin123"
            with type(self).connection() as conn:
                conn.execute(
                    "UPDATE accounts SET username = ?, password_hash = ? WHERE role = 'admin'",
                    ("admin", type(self).password_hash("admin123")),
                )
            type(self).mark_install_credentials_applied(("admin", "admin123"))

    def test_install_credentials_support_base64_env_values(self):
        encoded_username = base64.b64encode("base64-admin".encode("utf-8")).decode("ascii")
        encoded_password = base64.b64encode("p@ss word#$".encode("utf-8")).decode("ascii")
        os.environ["ADMIN_USERNAME_B64"] = encoded_username
        os.environ["ADMIN_PASSWORD_B64"] = encoded_password
        try:
            self.assertEqual(
                type(self).admin_credentials_from_env(),
                ("base64-admin", "p@ss word#$"),
            )
        finally:
            os.environ.pop("ADMIN_USERNAME_B64", None)
            os.environ.pop("ADMIN_PASSWORD_B64", None)

    def test_child_transactions_require_admin_approval(self):
        self.create_child(username="child", display_name="小朋友")
        earn_id = self.create_item("earn", "完成作业", 10)
        self.client.post("/api/auth/logout")
        response = self.client.post("/api/auth/login", json={"username": "child", "password": "child123"})
        self.assertEqual(response.get_json()["earn_items"][0]["id"], earn_id)

        response = self.client.post("/api/transactions", json={"kind": "earn", "item_id": earn_id})
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["request_submitted"])
        self.assertEqual(response.get_json()["total_points"], 0)
        request_id = response.get_json()["request_id"]
        self.assertEqual(self.client.post(f"/api/requests/{request_id}/approve").status_code, 403)

        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        requests = self.client.get("/api/state").get_json()["requests"]
        self.assertEqual(requests[0]["status"], "pending")
        response = self.client.post(f"/api/requests/{request_id}/approve")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["requests"][0]["status"], "approved")

        self.client.post("/api/auth/logout")
        response = self.client.post("/api/auth/login", json={"username": "child", "password": "child123"})
        self.assertEqual(response.get_json()["total_points"], 10)

    def test_v06_child_permissions_avatars_and_account_edit(self):
        self.create_child(username="child", display_name="小朋友")
        earn_id = self.create_item("earn", "完成作业", 10)
        state = self.state()
        self.assertEqual(state["version"], "0.8.18")
        self.assertEqual(state["user"]["avatar"], "adult-male")
        self.assertEqual(state["active_child"]["avatar"], "boy")

        self.client.post("/api/auth/logout")
        child_login = self.client.post("/api/auth/login", json={"username": "child", "password": "child123"})
        self.assertEqual(child_login.status_code, 200)
        self.assertEqual(child_login.get_json()["earn_items"][0]["id"], earn_id)
        self.assertEqual(self.client.post("/api/transactions", json={"kind": "deduct", "item_id": earn_id}).status_code, 403)
        self.assertEqual(self.client.post("/api/transactions", json={"kind": "manual", "points": -1, "name": "负数"}).status_code, 403)

        response = self.client.post("/api/transactions", json={"kind": "earn", "item_id": earn_id})
        self.assertEqual(response.status_code, 202)
        earn_request_id = response.get_json()["request_id"]
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        response = self.client.post(f"/api/requests/{earn_request_id}/approve")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["total_points"], 10)
        response = self.client.post("/api/items/reward", json={"name": "测试兑换", "points": 5})
        self.assertEqual(response.status_code, 201)
        reward_id = response.get_json()["id"]

        self.client.post("/api/auth/logout")
        child_login = self.client.post("/api/auth/login", json={"username": "child", "password": "child123"})
        response = self.client.post("/api/transactions", json={"kind": "exchange", "reward_id": reward_id})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["total_points"], 10)
        exchange_request_id = response.get_json()["request_id"]
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        response = self.client.post(f"/api/requests/{exchange_request_id}/approve")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["total_points"], 5)

        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        child_id = self.state()["active_child_id"]
        response = self.client.put(f"/api/accounts/{child_id}", json={"display_name": "新名字", "password": "newpass123", "avatar": "girl"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["display_name"], "新名字")
        self.assertEqual(response.get_json()["avatar"], "girl")
        self.assertIn("update_child", {log["action"] for log in self.state()["account_logs"]})

        self.client.post("/api/auth/logout")
        response = self.client.post("/api/auth/login", json={"username": "child", "password": "newpass123"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["user"]["display_name"], "新名字")
        self.assertEqual(response.get_json()["user"]["avatar"], "girl")

    def test_currency_settings_and_cash_exchange_require_approval(self):
        self.create_child(username="cash-child", display_name="现金孩子")
        state = self.state()
        self.assertEqual(state["settings"]["points_per_yuan"], 100)

        response = self.client.put("/api/settings", json={"points_per_yuan": 250})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["settings"]["points_per_yuan"], 250)

        response = self.client.post("/api/transactions", json={"kind": "earn", "points": 500, "name": "管理员发放"})
        self.assertEqual(response.status_code, 201)
        self.client.post("/api/auth/logout")
        self.assertEqual(
            self.client.post("/api/auth/login", json={"username": "cash-child", "password": "child123"}).status_code,
            200,
        )
        response = self.client.post("/api/transactions", json={"kind": "cash_exchange", "points": 250})
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["request_submitted"])
        self.assertEqual(response.get_json()["total_points"], 500)
        request_id = response.get_json()["request_id"]
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        response = self.client.post(f"/api/requests/{request_id}/approve")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["total_points"], 250)
        self.assertIn("兑换现金 ¥1.00", [record["title"] for record in response.get_json()["records"]])

    def test_adventure_level_can_be_managed_or_return_to_auto(self):
        self.create_child(username="level-child", display_name="等级孩子")
        response = self.client.put("/api/settings", json={"adventure_level": 7})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["settings"]["manual_adventure_level"], 7)
        self.assertEqual(data["settings"]["adventure_level_mode"], "manual")
        self.assertEqual(data["gamification"]["level"], 7)
        self.assertEqual(data["gamification"]["level_mode"], "manual")

        response = self.client.put("/api/settings", json={"adventure_level": "auto"})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIsNone(data["settings"]["manual_adventure_level"])
        self.assertEqual(data["settings"]["adventure_level_mode"], "auto")
        self.assertEqual(data["gamification"]["level"], 1)

    def test_level_benefits_raise_earnings_and_lower_exchange_cost(self):
        self.create_child(username="benefit-child", display_name="福利孩子")
        response = self.client.put("/api/settings", json={"adventure_level": 3})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["gamification"]["earn_bonus_percent"], 10)
        self.assertEqual(response.get_json()["gamification"]["exchange_discount_percent"], 5)

        earn = self.client.post("/api/items/earn", json={"name": "等级加金币", "points": 10, "icon": "points.svg"})
        self.assertEqual(earn.status_code, 201)
        reward = self.client.post("/api/items/reward", json={"name": "等级兑换", "points": 50, "icon": "gift.svg"})
        self.assertEqual(reward.status_code, 201)
        self.assertEqual(
            self.client.post("/api/transactions", json={"kind": "earn", "points": 100, "name": "准备兑换余额"}).status_code,
            201,
        )

        self.client.post("/api/auth/logout")
        self.assertEqual(
            self.client.post("/api/auth/login", json={"username": "benefit-child", "password": "child123"}).status_code,
            200,
        )
        data = self.state()
        earn_item = next(item for item in data["earn_items"] if item["name"] == "等级加金币")
        reward_item = next(item for item in data["rewards"] if item["name"] == "等级兑换")
        self.assertEqual(earn_item["base_points"], 10)
        self.assertEqual(earn_item["points"], 11)
        self.assertEqual(reward_item["base_points"], 50)
        self.assertEqual(reward_item["points"], 47)

        response = self.client.post("/api/transactions", json={"kind": "earn", "item_id": earn_item["id"]})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(next(item for item in response.get_json()["requests"] if item["title"] == "等级加金币")["amount"], 11)
        response = self.client.post("/api/transactions", json={"kind": "cash_exchange", "points": 50})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(next(item for item in response.get_json()["requests"] if item["kind"] == "cash_exchange")["amount"], -47)

        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).status_code, 200)
        response = self.client.post("/api/transactions", json={"kind": "earn", "points": 10, "name": "管理员发放"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["records"][0]["amount"], 10)

    def test_child_can_change_own_password(self):
        child = self.create_child(username="password-child", display_name="改密孩子")
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": child["username"], "password": "child123"})
        response = self.client.put(
            "/api/auth/password",
            json={"current_password": "wrong-password", "password": "newchild123", "password_confirm": "newchild123"},
        )
        self.assertEqual(response.status_code, 401)
        response = self.client.put(
            "/api/auth/password",
            json={"current_password": "child123", "password": "newchild123", "password_confirm": "newchild123"},
        )
        self.assertEqual(response.status_code, 200)
        self.client.post("/api/auth/logout")
        self.assertEqual(
            self.client.post("/api/auth/login", json={"username": child["username"], "password": "child123"}).status_code,
            401,
        )
        self.assertEqual(
            self.client.post("/api/auth/login", json={"username": child["username"], "password": "newchild123"}).status_code,
            200,
        )
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        self.assertIn("change_password", {log["action"] for log in self.state()["account_logs"]})

    def test_password_updates_and_last_child_can_be_deleted(self):
        child = self.create_child(username="child", display_name="小朋友")
        manager = self.client.post(
            "/api/accounts",
            json={"username": "manager2", "display_name": "家长二", "password": "manager123", "role": "admin"},
        ).get_json()

        response = self.client.put(
            f"/api/accounts/{manager['id']}",
            json={"display_name": "新家长", "password": "manager456", "avatar": "adult-female"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["avatar"], "adult-female")
        self.client.post("/api/auth/logout")
        self.assertEqual(
            self.client.post("/api/auth/login", json={"username": "manager2", "password": "manager123"}).status_code,
            401,
        )
        self.assertEqual(
            self.client.post("/api/auth/login", json={"username": "manager2", "password": "manager456"}).status_code,
            200,
        )
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})

        response = self.client.put(
            f"/api/accounts/{child['id']}",
            json={"display_name": "新名字", "password": "newpass123", "avatar": "girl"},
        )
        self.assertEqual(response.status_code, 200)
        self.client.post("/api/auth/logout")
        self.assertEqual(
            self.client.post("/api/auth/login", json={"username": "child", "password": "child123"}).status_code,
            401,
        )
        self.assertEqual(
            self.client.post("/api/auth/login", json={"username": "child", "password": "newpass123"}).status_code,
            200,
        )
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        self.assertEqual(self.client.delete(f"/api/accounts/{child['id']}").status_code, 200)
        self.assertIsNone(self.state()["active_child"])

    def test_deleting_account_clears_history_and_bindings(self):
        keeper = self.create_child(username="keeper", display_name="留下")
        gone = self.create_child(username="gone", display_name="删除")
        item_id = self.create_item("earn", "阅读", 4)

        self.client.post("/api/auth/select-child", json={"child_id": gone["id"]})
        self.client.put("/api/settings", json={"adventure_level": 5})
        self.assertEqual(self.client.post("/api/transactions", json={"kind": "earn", "item_id": item_id}).status_code, 201)
        self.client.patch(
            f"/api/item-bindings/{gone['id']}",
            json={"bindings": [{"kind": "earn", "item_id": item_id, "enabled": False}]},
        )
        self.client.post("/api/auth/select-child", json={"child_id": keeper["id"]})
        self.assertEqual(
            self.client.post("/api/transactions", json={"kind": "earn", "points": 6, "name": "自由加分"}).status_code,
            201,
        )

        def row_count(sql, account_id):
            with type(self).connection() as conn:
                return conn.execute(sql, (account_id,)).fetchone()[0]

        records_sql = "SELECT COUNT(*) FROM records WHERE account_id = ?"
        bindings_sql = "SELECT COUNT(*) FROM item_bindings WHERE account_id = ?"
        level_sql = "SELECT COUNT(*) FROM app_settings WHERE key = ?"
        self.assertEqual(row_count(records_sql, gone["id"]), 1)
        self.assertEqual(row_count(bindings_sql, gone["id"]), 1)
        self.assertEqual(row_count(level_sql, f"adventure_level:{gone['id']}"), 1)

        self.assertEqual(self.client.delete(f"/api/accounts/{gone['id']}").status_code, 200)

        self.assertEqual(row_count(records_sql, gone["id"]), 0)
        self.assertEqual(row_count(bindings_sql, gone["id"]), 0)
        self.assertEqual(row_count(level_sql, f"adventure_level:{gone['id']}"), 0)
        self.assertEqual(row_count(records_sql, keeper["id"]), 1)
        self.assertIn(item_id, [row["id"] for row in self.state()["earn_items"]])

    def test_clearing_history_also_drops_settlement_watermarks(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.client.post(
            "/api/items/earn",
            json={"name": "水位项目", "points": 5, "icon": "book.svg", "week_target": 5, "week_penalty": 3},
        ).get_json()
        self.grant(item["id"], weeks_back=1)
        self.backdate_rule(item["id"], self.monday(1))
        self.settle()
        self.assertTrue(self.settlement_rows(item["id"]))

        # 结算是从记录倒推出来的，所以清空孩子的历史必须连结算水位一起清掉。
        self.assertEqual(self.client.post("/api/system/clear").status_code, 200)
        self.assertEqual(self.settlement_rows(item["id"]), [])
        self.assertEqual(self.state()["total_points"], 0)
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_earn_deduct_exchange_and_undo(self):
        self.create_child()
        earn_id = self.create_item("earn", "完成作业", 10)
        deduct_id = self.create_item("deduct", "迟到", 5)
        self.assertEqual(self.client.post("/api/transactions", json={"kind": "earn", "item_id": earn_id}).status_code, 201)
        response = self.client.post("/api/transactions", json={"kind": "deduct", "item_id": deduct_id})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["total_points"], 5)

        reward_id = self.create_item("reward", "买零食", 20)
        self.assertEqual(self.client.post("/api/transactions", json={"kind": "exchange", "reward_id": reward_id}).status_code, 409)

        newest = self.state()["records"][0]
        # 行内的撤销要把记录认回项目行，所以载荷必须带上项目标识
        self.assertEqual((newest["item_kind"], newest["item_id"]), ("deduct", deduct_id))
        response = self.client.post(f"/api/transactions/{newest['id']}/undo")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["total_points"], 10)

    def test_note_keeps_the_item_name_and_marks_a_补录(self):
        self.create_child()
        item_id = self.create_item("earn", "阅读", 5)
        self.assertEqual(
            self.client.post(
                "/api/transactions", json={"kind": "earn", "item_id": item_id, "name": "读了课外书"}
            ).status_code,
            201,
        )
        # 备注是唯一能把这条记录和光板的项目记录分开的线索，日列表单看认不出来。
        self.assertEqual(self.state()["records"][0]["title"], "阅读（读了课外书）")
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        self.assertEqual(
            self.manual_entry(item_kind="earn", item_id=item_id, name="漏记了", date=yesterday).status_code,
            201,
        )
        self.assertEqual(self.state()["records"][0]["title"], "补录：阅读（漏记了）")
        # 项目行的角标读的是列表上方选中的日期，所以补录那天带着自己的计数。
        row = self.earn_item_row(item_id)
        self.assertEqual((row["day_counts"][yesterday], row["day_counts"][date.today().isoformat()]), (1, 1))

    def test_both_entry_lists_carry_the_tally_of_a_backfilled_day(self):
        self.create_child()
        earn_id = self.create_item("earn", "整理房间", 4)
        deduct_id = self.create_item("deduct", "迟到", 5)
        day = (date.today() - timedelta(days=9)).isoformat()
        self.client.post("/api/transactions", json={"kind": "earn", "item_id": earn_id, "date": day})
        self.deduct_on(deduct_id, date.fromisoformat(day))
        rows = self.state()
        earn = next(row for row in rows["earn_items"] if row["id"] == earn_id)
        deduct = next(row for row in rows["deduct_items"] if row["id"] == deduct_id)
        self.assertEqual((earn["day_counts"][day], deduct["day_counts"][day]), (1, 1))

    def test_custom_items_and_manual_history(self):
        self.create_child()
        response = self.client.post("/api/items/earn", json={"name": "整理书桌", "points": 6, "icon": "computer.svg"})
        self.assertEqual(response.status_code, 201)
        item_id = response.get_json()["id"]
        self.assertEqual(response.get_json()["icon"], "computer.svg")

        response = self.client.put(f"/api/items/earn/{item_id}", json={"name": "整理书桌并复习", "points": 9, "icon": "game.svg"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["name"], "整理书桌并复习")
        self.assertEqual(response.get_json()["icon"], "game.svg")

        response = self.client.post("/api/items/earn", json={"name": "做饭", "points": 8, "icon": "cooking.svg"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["icon"], "cooking.svg")
        self.assertEqual(
            self.client.post("/api/items/earn", json={"name": "不使用的图标", "points": 8, "icon": "camera.svg"}).status_code,
            400,
        )
        self.assertEqual(
            self.client.post("/api/items/earn", json={"name": "水果", "points": 8, "icon": "apple.svg"}).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                "/api/items/earn",
                json={"name": "不允许的远程图标", "points": 8, "icon": "iconify:fluent-emoji:pot-of-food"},
            ).status_code,
            400,
        )

        response = self.client.post("/api/transactions", json={"kind": "manual", "name": "历史补录", "points": 12, "date": "2026-01-02"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["records"][0]["date"], "2026-01-02")

        self.assertEqual(self.client.delete(f"/api/items/earn/{item_id}").status_code, 200)

    def test_life_icons_are_shared_by_earn_deduct_and_reward(self):
        self.create_child()
        for kind, name in (("earn", "洗衣"), ("deduct", "洗碗"), ("reward", "购物")):
            response = self.client.post(
                f"/api/items/{kind}",
                json={"name": name, "points": 8, "icon": "laundry.svg"},
            )
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.get_json()["icon"], "laundry.svg")

    def test_rpg_task_lifecycle_rewards_coins_experience_and_achievement(self):
        child = self.create_child(username="questkid", display_name="任务玩家", password="quest123")
        response = self.client.post(
            "/api/tasks",
            json={
                "title": "整理自己的书桌",
                "description": "清理完成后提交验收",
                "category": "家庭",
                "task_type": "epic",
                "difficulty": "hard",
                "reward_coins": 40,
                "reward_exp": 25,
                "icon": "computer.svg",
            },
        )
        self.assertEqual(response.status_code, 201)
        task_id = response.get_json()["task"]["id"]

        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.post("/api/auth/login", json={"username": "questkid", "password": "quest123"}).status_code, 200)
        response = self.client.post(f"/api/tasks/{task_id}/claim")
        self.assertEqual(response.status_code, 201)
        assignment_id = response.get_json()["assignment_id"]
        self.assertEqual(self.client.post(f"/api/task-assignments/{assignment_id}/submit").status_code, 200)

        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).status_code, 200)
        response = self.client.post(f"/api/task-assignments/{assignment_id}/approve")
        self.assertEqual(response.status_code, 200)

        self.client.post("/api/auth/logout")
        response = self.client.post("/api/auth/login", json={"username": "questkid", "password": "quest123"})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["gamification"]["coins"], 40)
        self.assertEqual(data["gamification"]["experience"], 25)
        self.assertTrue(any(item["achievement_key"] == "first-quest" and item["unlocked"] for item in data["achievements"]))

    def test_reclaiming_a_task_rejected_today_reports_conflict(self):
        self.create_child(username="redokid", display_name="重做玩家", password="quest123")
        task_id = self.client.post(
            "/api/tasks",
            json={"title": "倒垃圾", "task_type": "daily", "difficulty": "easy", "reward_coins": 5, "reward_exp": 2},
        ).get_json()["task"]["id"]
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "redokid", "password": "quest123"})
        assignment_id = self.client.post(f"/api/tasks/{task_id}/claim").get_json()["assignment_id"]
        self.assertEqual(self.client.post(f"/api/task-assignments/{assignment_id}/submit").status_code, 200)
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        self.assertEqual(self.client.post(f"/api/task-assignments/{assignment_id}/reject").status_code, 200)
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "redokid", "password": "quest123"})
        response = self.client.post(f"/api/tasks/{task_id}/claim")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "你已经领取过这个任务")
        self.assertEqual(self.client.post(f"/api/task-assignments/{assignment_id}/submit").status_code, 200)

    def test_editing_a_task_keeps_omitted_fields_and_deleting_hides_it(self):
        self.create_child(username="questeditor", display_name="改单玩家", password="quest123")
        task_id = self.client.post(
            "/api/tasks",
            json={
                "title": "倒垃圾",
                "description": "袋子换好放回原位",
                "category": "家庭",
                "task_type": "daily",
                "difficulty": "easy",
                "reward_coins": 10,
                "reward_exp": 4,
                "icon": "cleaning.svg",
                "due_date": "2026-12-31",
            },
        ).get_json()["task"]["id"]

        updated = self.client.put(f"/api/tasks/{task_id}", json={"title": "倒垃圾并换袋", "reward_coins": 16})
        self.assertEqual(updated.status_code, 200)
        task = next(item for item in updated.get_json()["state"]["tasks"] if item["id"] == task_id)
        self.assertEqual(task["title"], "倒垃圾并换袋")
        self.assertEqual(task["reward_coins"], 16)
        self.assertEqual(task["description"], "袋子换好放回原位")
        self.assertEqual(task["icon"], "cleaning.svg")
        self.assertEqual(task["difficulty"], "easy")
        # 编辑是部分更新：载荷没带的截止日期要沿用库里那行，不能被清空。
        self.assertEqual(task["due_date"], "2026-12-31")

        deleted = self.client.delete(f"/api/tasks/{task_id}")
        self.assertTrue(deleted.get_json()["deleted"])
        self.assertNotIn(task_id, [item["id"] for item in deleted.get_json()["state"]["tasks"]])
        self.assertEqual(self.client.put(f"/api/tasks/{task_id}", json={"title": "改名"}).status_code, 404)
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "questeditor", "password": "quest123"})
        self.assertEqual(self.client.post(f"/api/tasks/{task_id}/claim").status_code, 404)

    def test_builtin_package_icon_is_allowed_and_unknown_icon_is_rejected(self):
        self.create_child(username="icon-child", display_name="Icon Child", password="child123")
        response = self.client.post(
            "/api/items/earn",
            json={"name": "快递取件", "points": 8, "icon": "emoji:package"},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["icon"], "emoji:package")
        self.assertEqual(
            self.client.post(
                "/api/items/earn",
                json={"name": "未知图标", "points": 8, "icon": "emoji:not-a-real-icon"},
            ).status_code,
            400,
        )

    def test_announcements_can_be_published_edited_removed_and_seen_by_children(self):
        child = self.create_child(username="notice-child", display_name="Notice Child", password="child123")
        response = self.client.post(
            "/api/announcements",
            json={"title": "本周悬赏", "content": "完成家庭挑战领取金币", "audience": "children"},
        )
        self.assertEqual(response.status_code, 201)
        announcement_id = response.get_json()["announcement_id"]
        self.assertEqual(response.get_json()["announcements"][0]["title"], "本周悬赏")

        response = self.client.put(
            f"/api/announcements/{announcement_id}",
            json={"title": "更新后的悬赏", "content": "今晚前提交任务", "audience": "all"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["announcements"][0]["audience"], "all")

        self.client.post("/api/auth/logout")
        self.assertEqual(
            self.client.post("/api/auth/login", json={"username": child["username"], "password": "child123"}).status_code,
            200,
        )
        self.assertEqual(self.state()["announcements"][0]["title"], "更新后的悬赏")

        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        self.assertEqual(self.client.delete(f"/api/announcements/{announcement_id}").status_code, 200)
        self.assertEqual(self.state()["announcements"], [])

    def test_clear_and_reset(self):
        self.create_child()
        self.client.post("/api/transactions", json={"kind": "earn", "points": 20, "name": "测试"})
        self.assertEqual(self.client.post("/api/system/clear").get_json()["total_points"], 0)
        self.client.post("/api/items/reward", json={"name": "临时奖励", "points": 1})
        data = self.client.post("/api/system/reset").get_json()
        self.assertEqual(data["total_points"], 0)
        self.assertEqual(len(data["rewards"]), len(data["item_library"]["reward"]))

    def test_clear_points_needs_a_child_account(self):
        self.assertIsNone(self.state()["active_child"])
        response = self.client.post("/api/system/clear")
        self.assertEqual(response.status_code, 400)
        self.assertIn("当前没有可用的孩子账号", response.get_json()["error"])

    def test_item_library_is_system_wide(self):
        first = self.create_child(username="kid-a")
        second = self.create_child(username="kid-b")
        item = self.client.post("/api/items/earn", json={"name": "系统项目", "points": 7, "icon": "book.svg"}).get_json()
        for child in (first, second):
            self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
            self.assertIn(item["id"], [row["id"] for row in self.state()["earn_items"]])

        response = self.client.put(
            f"/api/item-bindings/{second['id']}",
            json={"bindings": [{"kind": "earn", "item_id": item["id"], "enabled": False}]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(item["id"], [row["id"] for row in response.get_json()["earn_items"]])
        self.client.post("/api/auth/select-child", json={"child_id": first["id"]})
        self.assertIn(item["id"], [row["id"] for row in self.state()["earn_items"]])

        self.client.post("/api/auth/select-child", json={"child_id": second["id"]})
        self.assertEqual(
            self.client.post("/api/transactions", json={"kind": "earn", "item_id": item["id"]}).status_code, 400
        )
        self.client.put(
            f"/api/item-bindings/{second['id']}",
            json={"bindings": [{"kind": "earn", "item_id": item["id"], "enabled": True}]},
        )
        response = self.client.post("/api/transactions", json={"kind": "earn", "item_id": item["id"]})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["total_points"], 7)

        response = self.client.patch(
            f"/api/item-bindings/{second['id']}",
            json={"bindings": [{"kind": "earn", "item_id": item["id"], "enabled": False}]},
        )
        self.assertNotIn(item["id"], [row["id"] for row in response.get_json()["earn_items"]])
        self.client.put(f"/api/item-bindings/{second['id']}", json={"action": "reset"})
        self.assertIn(item["id"], [row["id"] for row in self.state()["earn_items"]])

        self.assertEqual(self.client.delete(f"/api/items/earn/{item['id']}").status_code, 200)
        self.client.post("/api/auth/select-child", json={"child_id": first["id"]})
        self.assertNotIn(item["id"], [row["id"] for row in self.state()["earn_items"]])

    def test_item_rule_override_only_changes_that_child(self):
        shared = self.create_child(username="kid-shared")
        own = self.create_child(username="kid-own")
        item = self.client.post("/api/items/earn", json={"name": "覆盖项目", "points": 5, "icon": "book.svg"}).get_json()

        response = self.client.put(
            f"/api/item-rules/{own['id']}",
            json={"kind": "earn", "item_id": item["id"], "week_target": 1, "week_bonus": 4, "cap_period": "day", "cap_count": 1},
        )
        self.assertEqual(response.status_code, 200)
        rules = response.get_json()["item_rules"]
        self.assertEqual(rules[str(shared["id"])]["earn"], {})
        self.assertEqual(rules[str(own["id"])]["earn"][str(item["id"])]["week_target"], 1)
        self.assertEqual(self.client.put(f"/api/item-rules/{own['id']}", json={"kind": "reward", "item_id": item["id"]}).status_code, 404)

        self.client.post("/api/auth/select-child", json={"child_id": shared["id"]})
        self.assertEqual(self.earn_item_row(item["id"])["week_target"], 0)
        self.client.post("/api/auth/select-child", json={"child_id": own["id"]})
        row = self.earn_item_row(item["id"])
        self.assertEqual((row["week_target"], row["week_bonus"], row["cap_period"], row["cap_count"]), (1, 4, "day", 1))
        # 提交上限和规则走同一层合并，所以只拦带上这个上限的孩子。
        for expected in (201, 400):
            self.assertEqual(self.client.post("/api/transactions", json={"kind": "earn", "item_id": item["id"]}).status_code, expected)
        self.client.post("/api/auth/select-child", json={"child_id": shared["id"]})
        for _ in range(2):
            self.assertEqual(self.client.post("/api/transactions", json={"kind": "earn", "item_id": item["id"]}).status_code, 201)
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_item_rule_override_settles_only_for_that_child(self):
        plain = self.create_child(username="kid-plain")
        target = self.create_child(username="kid-target")
        item = self.client.post("/api/items/earn", json={"name": "每孩子的目标", "points": 5, "icon": "book.svg"}).get_json()
        self.client.put(
            f"/api/item-rules/{target['id']}",
            json={"kind": "earn", "item_id": item["id"], "week_target": 2, "week_bonus": 4},
        )
        self.backdate_rule(item["id"], self.monday(1), account_id=target["id"])
        for child in (plain, target):
            self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
            self.grant(item["id"], weeks_back=1, times=3)

        # 最后一条记录已当场重结那一整周，手动结算再走一遍就无可发的了。
        self.assertEqual(self.settle()["settlement"]["entries"], 0)
        self.client.post("/api/auth/select-child", json={"child_id": target["id"]})
        self.assertEqual([row["amount"] for row in self.state()["records"] if row["source"] == "weekly_bonus"], [4])
        self.client.post("/api/auth/select-child", json={"child_id": plain["id"]})
        self.assertEqual([row for row in self.state()["records"] if row["source"] == "weekly_bonus"], [])
        self.assertEqual(len(self.settlement_rows(item["id"])), 1)
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_period_table_puts_children_in_columns(self):
        plain = self.create_child(username="kid-plain")
        target = self.create_child(username="kid-quantity")
        item = self.client.post("/api/items/earn", json={"name": "两种口径", "points": 3, "icon": "book.svg"}).get_json()
        self.client.put(
            f"/api/item-rules/{target['id']}",
            json={"kind": "earn", "item_id": item["id"], "target_measure": "quantity", "week_target": 1},
        )
        self.client.post("/api/auth/select-child", json={"child_id": plain["id"]})
        self.assertEqual(self.client.post("/api/transactions", json={"kind": "earn", "item_id": item["id"]}).status_code, 201)
        self.client.post("/api/auth/select-child", json={"child_id": target["id"]})
        self.assertEqual(
            self.client.post("/api/transactions", json={"kind": "earn", "item_id": item["id"], "quantity": 4}).status_code,
            201,
        )

        table = self.client.get("/api/analytics?days=7").get_json()["item_periods"]
        rows = [row for row in table["items"]["earn"] if row["item_id"] == item["id"]]
        # 同一个项目只占一行，两个孩子各占一列；量词跟着各自的生效规则走。
        self.assertEqual(len(rows), 1)
        columns = [child["id"] for child in table["children"]]
        cells = {columns[index]: cell for index, cell in enumerate(rows[0]["cells"])}
        self.assertEqual(
            (cells[plain["id"]]["measure"], cells[plain["id"]]["count"], cells[plain["id"]]["quantity"], cells[plain["id"]]["coins"]),
            ("count", 1, 0, 3),
        )
        self.assertEqual(
            (cells[target["id"]]["measure"], cells[target["id"]]["count"], cells[target["id"]]["quantity"], cells[target["id"]]["coins"]),
            ("quantity", 1, 4, 0),
        )
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_project_table_counts_one_submission_per_day(self):
        child = self.create_child(username="kid-once-a-day")
        item = self.client.post(
            "/api/items/earn",
            json={"name": "一日一次", "points": 2, "icon": "book.svg", "week_target": 2, "count_days_once": 1},
        ).get_json()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        for _ in range(3):
            self.assertEqual(self.submit_earn(item["id"]).status_code, 201)

        table = self.client.get("/api/analytics?days=7").get_json()["item_periods"]
        row = next(row for row in table["items"]["earn"] if row["item_id"] == item["id"])
        position = [child_row["id"] for child_row in table["children"]].index(child["id"])
        cell = row["cells"][position]
        # 与角标、目标表同一口径：同一天三次只算 1 次，积分仍按三笔计。
        self.assertEqual((cell["count"], cell["coins"]), (1, 6))
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_item_rule_copy_takes_the_source_childs_effective_rule(self):
        source = self.create_child(username="kid-source")
        target = self.create_child(username="kid-target")
        item = self.client.post(
            "/api/items/earn", json={"name": "抄规则项目", "points": 5, "icon": "book.svg", "week_target": 3, "week_bonus": 2}
        ).get_json()
        self.client.put(
            f"/api/item-rules/{source['id']}",
            json={"kind": "earn", "item_id": item["id"], "week_target": 1, "week_bonus": 9, "cap_period": "day", "cap_count": 1},
        )

        self.assertEqual(
            self.client.put(
                f"/api/item-rules/{source['id']}",
                json={"action": "copy", "kind": "earn", "item_id": item["id"], "copy_from": source["id"]},
            ).status_code,
            400,
        )
        response = self.client.put(
            f"/api/item-rules/{target['id']}",
            json={"action": "copy", "kind": "earn", "item_id": item["id"], "copy_from": source["id"]},
        )
        self.assertEqual(response.status_code, 200)
        self.client.post("/api/auth/select-child", json={"child_id": target["id"]})
        row = self.earn_item_row(item["id"])
        self.assertEqual((row["week_target"], row["week_bonus"], row["cap_period"], row["cap_count"]), (1, 9, "day", 1))

        reset = self.client.delete(f"/api/item-rules/{target['id']}", json={"kind": "earn", "item_id": item["id"]})
        self.assertEqual(reset.status_code, 200)
        row = self.earn_item_row(item["id"])
        self.assertEqual((row["week_target"], row["week_bonus"], row["cap_period"]), (3, 2, ""))
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_copying_an_unruled_item_still_anchors_the_snapshot(self):
        source = self.create_child(username="kid-blank")
        target = self.create_child(username="kid-copy")
        item = self.client.post("/api/items/earn", json={"name": "无规则项目", "points": 4, "icon": "book.svg"}).get_json()
        response = self.client.put(
            f"/api/item-rules/{target['id']}",
            json={"action": "copy", "kind": "earn", "item_id": item["id"], "copy_from": source["id"]},
        )
        self.assertEqual(response.status_code, 200)
        snapshot = response.get_json()["item_rules"][str(target["id"])]["earn"][str(item["id"])]
        self.assertEqual(snapshot["week_target"], 0)
        self.assertTrue(snapshot["rule_set_at"])
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_bulk_rule_copy_snapshots_the_library_and_clears_bindings(self):
        first = self.create_child(username="kid-first")
        second = self.create_child(username="kid-second")
        earn = self.client.post(
            "/api/items/earn", json={"name": "整库加分", "points": 5, "icon": "book.svg", "week_target": 2, "week_bonus": 3}
        ).get_json()
        deduct = self.client.post("/api/items/deduct", json={"name": "整库扣分", "points": 2}).get_json()
        reward = self.client.post("/api/items/reward", json={"name": "整库奖励", "points": 10}).get_json()
        for child in (first, second):
            self.client.put(
                f"/api/item-bindings/{child['id']}",
                json={"bindings": [
                    {"kind": "deduct", "item_id": deduct["id"], "enabled": False},
                    {"kind": "reward", "item_id": reward["id"], "enabled": False},
                ]},
            )

        response = self.client.post(
            "/api/item-rules/copy",
            json={"source_id": None, "target_ids": [first["id"]], "include_bindings": True},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        data = response.get_json()
        rules = data["item_rules"][str(first["id"])]
        self.assertEqual(rules["earn"][str(earn["id"])]["week_target"], 2)
        self.assertEqual(rules["earn"][str(earn["id"])]["week_bonus"], 3)
        self.assertIn(str(deduct["id"]), rules["deduct"])
        self.assertEqual(data["item_bindings"][str(first["id"])], {"earn": [], "deduct": [], "reward": []})
        # 没点名的孩子仍然只跟着库走，规则快照与被排除的项目都没变。
        self.assertEqual(data["item_rules"][str(second["id"])]["earn"], {})
        self.assertEqual(data["item_bindings"][str(second["id"])]["deduct"], [deduct["id"]])
        self.client.delete(f"/api/items/earn/{earn['id']}")
        self.client.delete(f"/api/items/deduct/{deduct['id']}")
        self.client.delete(f"/api/items/reward/{reward['id']}")

    def test_bulk_rule_copy_takes_rules_and_bindings_from_a_child(self):
        source = self.create_child(username="kid-source")
        first = self.create_child(username="kid-first")
        second = self.create_child(username="kid-second")
        earn = self.client.post(
            "/api/items/earn", json={"name": "抄整套", "points": 4, "icon": "book.svg", "week_target": 3, "week_bonus": 1}
        ).get_json()
        deduct = self.client.post("/api/items/deduct", json={"name": "抄可见范围", "points": 2}).get_json()
        self.client.put(
            f"/api/item-rules/{source['id']}",
            json={"kind": "earn", "item_id": earn["id"], "week_target": 1, "week_bonus": 9, "cap_period": "day", "cap_count": 1},
        )
        self.client.put(
            f"/api/item-bindings/{source['id']}",
            json={"bindings": [{"kind": "deduct", "item_id": deduct["id"], "enabled": False}]},
        )
        self.client.post("/api/auth/select-child", json={"child_id": first["id"]})
        self.client.put(f"/api/item-rules/{first['id']}", json={"kind": "earn", "item_id": earn["id"], "week_target": 2})

        response = self.client.post(
            "/api/item-rules/copy",
            json={"source_id": source["id"], "target_ids": [first["id"], second["id"]], "include_bindings": True},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        data = response.get_json()
        for child in (first, second):
            snapshot = data["item_rules"][str(child["id"])]["earn"][str(earn["id"])]
            self.assertEqual((snapshot["week_target"], snapshot["week_bonus"], snapshot["cap_period"], snapshot["cap_count"]), (1, 9, "day", 1))
            self.assertEqual(data["item_bindings"][str(child["id"])]["deduct"], [deduct["id"]])
        # 快照自带锚点，抄过去就能按同一套规则结算。
        self.assertTrue(data["item_rules"][str(second["id"])]["earn"][str(earn["id"])]["rule_set_at"])
        self.client.delete(f"/api/items/earn/{earn['id']}")
        self.client.delete(f"/api/items/deduct/{deduct['id']}")

    def test_bulk_rule_copy_validates_every_target_before_writing(self):
        source = self.create_child(username="kid-source")
        target = self.create_child(username="kid-target")
        item = self.client.post("/api/items/earn", json={"name": "原子复制", "points": 3}).get_json()
        self.client.put(f"/api/item-rules/{source['id']}", json={"kind": "earn", "item_id": item["id"], "week_target": 1})

        for payload, status, message in (
            ({"source_id": source["id"], "target_ids": []}, 400, "请选择"),
            ({"source_id": source["id"], "target_ids": [target["id"], target["id"]]}, 400, "重复"),
            ({"source_id": source["id"], "target_ids": [source["id"]]}, 400, "来源不能"),
            ({"source_id": source["id"] + 999, "target_ids": [target["id"]]}, 404, "来源孩子不存在"),
            ({"source_id": source["id"], "target_ids": [target["id"], target["id"] + 999]}, 404, "目标孩子不存在"),
        ):
            response = self.client.post("/api/item-rules/copy", json=payload)
            self.assertEqual(response.status_code, status, payload)
            self.assertIn(message, response.get_json()["error"])

        # 上面那批失败请求里，合法目标一笔都没落盘。
        self.assertEqual(self.state()["item_rules"][str(target["id"])]["earn"], {})
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": target["username"], "password": "child123"})
        response = self.client.post("/api/item-rules/copy", json={"source_id": source["id"], "target_ids": [target["id"]]})
        self.assertEqual(response.status_code, 403)
        self.client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_item_bindings_reject_the_whole_batch_when_one_entry_is_bad(self):
        child = self.create_child(username="kid-batch")
        earn = self.client.post("/api/items/earn", json={"name": "批量项目", "points": 3}).get_json()
        response = self.client.put(
            f"/api/item-bindings/{child['id']}",
            json={
                "bindings": [
                    {"kind": "earn", "item_id": earn["id"], "enabled": False},
                    {"kind": "earn", "item_id": earn["id"] + 9999, "enabled": False},
                ]
            },
        )
        self.assertEqual(response.status_code, 404)
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        self.assertIn(earn["id"], [row["id"] for row in self.state()["earn_items"]])

    def test_duplicate_item_names_are_rejected(self):
        first = self.client.post("/api/items/earn", json={"name": "阅读 20 分钟", "points": 2}).get_json()
        duplicate = self.client.post("/api/items/earn", json={"name": "阅读 20 分钟", "points": 5})
        self.assertEqual(duplicate.status_code, 400)
        self.assertIn("同名", duplicate.get_json()["error"])
        self.assertEqual(
            self.client.post("/api/items/deduct", json={"name": "阅读 20 分钟", "points": 1}).status_code, 201
        )
        second = self.client.post("/api/items/earn", json={"name": "计算练习", "points": 3}).get_json()
        conflict = self.client.put(f"/api/items/earn/{second['id']}", json={"name": "阅读 20 分钟", "points": 3})
        self.assertEqual(conflict.status_code, 400)
        kept = self.client.put(f"/api/items/earn/{first['id']}", json={"name": "阅读 20 分钟", "points": 4})
        self.assertEqual(kept.status_code, 200)
        self.assertEqual(kept.get_json()["points"], 4)

    def earn_item_row(self, item_id):
        return next(item for item in self.state()["earn_items"] if item["id"] == item_id)

    @staticmethod
    def monday(weeks_back=0):
        today = date.today()
        return today - timedelta(days=today.weekday() + weeks_back * 7)

    def backdate_rule(self, item_id, day, kind="earn", account_id=None):
        """把项目库规则、或某个孩子在它上面的快照，锚到被测窗口的起点之前。"""
        if account_id is None:
            sql, tail = f"UPDATE {kind}_items SET rule_set_at = ? WHERE id = ?", (item_id,)
        else:
            sql, tail = (
                "UPDATE item_rule_overrides SET rule_set_at = ? WHERE item_kind = ? AND item_id = ? AND account_id = ?",
                (kind, item_id, account_id),
            )
        with type(self).connection() as conn:
            conn.execute(sql, (f"{day.isoformat()}T00:00:00", *tail))

    def settlement_rows(self, item_id, kind="earn"):
        with type(self).connection() as conn:
            return conn.execute(
                "SELECT week_start, bonus, penalty FROM weekly_settlements WHERE item_kind = ? AND item_id = ? ORDER BY week_start",
                (kind, item_id),
            ).fetchall()

    def watermarks(self, item_id, kind="earn"):
        """取已定级的窗口，形如 (week_start, bonus, penalty)，写入前后拿同一份坐标对比。"""
        return [(row["week_start"], row["bonus"], row["penalty"]) for row in self.settlement_rows(item_id, kind)]

    def deduct_on(self, item_id, day, times=1):
        for _ in range(times):
            response = self.client.post(
                "/api/transactions", json={"kind": "deduct", "item_id": item_id, "date": day.isoformat()}
            )
            self.assertEqual(response.status_code, 201)

    def grant(self, item_id, weeks_back=1, times=1):
        day = date.today() if weeks_back == 0 else self.monday(weeks_back) + timedelta(days=1)
        for _ in range(times):
            response = self.client.post(
                "/api/transactions", json={"kind": "earn", "item_id": item_id, "date": day.isoformat()}
            )
            self.assertEqual(response.status_code, 201)

    def settle(self, unit="week", period=None, force=False):
        """比一天长的窗口只在被点名时才结算，所以用例要自己指定那一期。"""
        payload = {"unit": unit, "force": force}
        if period:
            payload["period"] = period
        response = self.client.post("/api/settlements/run", json=payload)
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def settle_flags(self, unit="week"):
        """取结算期次下拉每一格的 (closed, settled)，用例据此跟踪窗口的定级过程。"""
        rows = self.client.get(f"/api/settlements/periods?unit={unit}").get_json()["periods"]
        return {row["start"]: (row["closed"], row["settled"]) for row in rows}

    def test_weekly_rule_counts_cover_today_and_this_week(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.client.post(
            "/api/items/earn", json={"name": "计数项目", "points": 6, "icon": "book.svg"}
        ).get_json()
        self.assertEqual(item["week_target"], 0)
        for _ in range(2):
            self.assertEqual(self.client.post("/api/transactions", json={"kind": "earn", "item_id": item["id"]}).status_code, 201)

        row = self.earn_item_row(item["id"])
        self.assertEqual(row["day_counts"][date.today().isoformat()], 2)
        self.assertEqual(row["week_count"], 2)

        self.grant(item["id"], weeks_back=1)
        row = self.earn_item_row(item["id"])
        self.assertEqual(row["day_counts"][(self.monday(1) + timedelta(days=1)).isoformat()], 1)
        self.assertEqual(row["day_counts"][date.today().isoformat()], 2)
        self.assertEqual(row["week_count"], 2)
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_weekly_rule_over_target_bonus_settles_once(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.client.post(
            "/api/items/earn",
            json={"name": "超额项目", "points": 5, "icon": "book.svg", "week_target": 2, "week_bonus": 4},
        ).get_json()
        self.assertEqual(item["week_bonus"], 4)
        self.backdate_rule(item["id"], self.monday(1))
        self.grant(item["id"], weeks_back=1, times=3)

        data = self.settle()
        bonuses = [row for row in data["records"] if row["source"] == "weekly_bonus"]
        self.assertEqual([row["amount"] for row in bonuses], [4])
        self.assertEqual(data["total_points"], 3 * 5 + 4)
        self.assertEqual(len(self.settlement_rows(item["id"])), 1)
        self.assertEqual(len(self.settle()["records"]), len(data["records"]))
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_weekly_rule_below_target_penalty_and_watermark(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.client.post(
            "/api/items/earn",
            json={"name": "达标项目", "points": 5, "icon": "book.svg", "week_target": 5, "week_penalty": 3},
        ).get_json()
        self.grant(item["id"], weeks_back=2)
        self.backdate_rule(item["id"], self.monday(1))
        self.grant(item["id"], weeks_back=1)

        data = self.settle()
        penalties = [row for row in data["records"] if row["source"] == "weekly_penalty"]
        self.assertEqual([row["title"] for row in penalties], ["达标项目 1/5"])
        self.assertEqual(data["total_points"], 2 * 5 - 3)
        self.assertEqual([row["week_start"] for row in self.settlement_rows(item["id"])], [self.monday(1).isoformat()])
        self.assertEqual(len(self.settle()["records"]), len(data["records"]))
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_target_rule_counts_two_week_windows(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        app = self.app_module
        current = app.period_start("week", date.today().isoformat(), 2)
        previous = app.period_shift("week", 2, current, -1)
        earliest = app.period_shift("week", 2, current, -2)
        self.assertEqual((date.fromisoformat(previous) - date.fromisoformat(earliest)).days, 14)
        item = self.client.post(
            "/api/items/earn",
            json={
                "name": "双周项目",
                "points": 5,
                "icon": "book.svg",
                "week_target": 3,
                "week_penalty": 3,
                "target_unit": "week",
                "target_every": 2,
            },
        ).get_json()
        self.assertEqual(item["target_every"], 2)
        self.backdate_rule(item["id"], date.fromisoformat(earliest))
        self.grant(item["id"], weeks_back=4)
        self.settle()
        rows = self.settlement_rows(item["id"])
        self.assertEqual([row["week_start"] for row in rows], [earliest, previous])
        self.assertEqual([row["penalty"] for row in rows], [3, 3])
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_settlement_skips_items_unbound_for_the_child(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.client.post(
            "/api/items/earn",
            json={"name": "解绑项目", "points": 5, "icon": "book.svg", "week_target": 5, "week_penalty": 3},
        ).get_json()
        self.grant(item["id"], weeks_back=1)
        self.backdate_rule(item["id"], self.monday(1))
        self.client.put(
            f"/api/item-bindings/{child['id']}",
            json={"bindings": [{"kind": "earn", "item_id": item["id"], "enabled": False}]},
        )

        # 项目从这个孩子解绑后既不加分也不扣分，哪怕历史上还留着记录。
        self.settle()
        self.assertEqual(self.settlement_rows(item["id"]), [])
        self.assertEqual([row for row in self.state()["records"] if row["source"] == "weekly_penalty"], [])

        self.client.put(
            f"/api/item-bindings/{child['id']}",
            json={"bindings": [{"kind": "earn", "item_id": item["id"], "enabled": True}]},
        )
        self.assertEqual(
            [row["amount"] for row in self.settle()["records"] if row["source"] == "weekly_penalty"],
            [-3],
        )
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_shortfall_fine_starts_at_the_first_logged_window(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.client.post(
            "/api/items/earn",
            json={"name": "早起项目", "points": 5, "icon": "book.svg", "week_target": 5, "week_penalty": 3},
        ).get_json()
        self.grant(item["id"], weeks_back=1)
        # 规则回看到六周之前，但这个孩子唯一的记录只在上周。
        self.backdate_rule(item["id"], self.monday(6))
        self.assertEqual(
            [row["date"] for row in self.settle()["records"] if row["source"] == "weekly_penalty"],
            [(self.monday(1) + timedelta(days=6)).isoformat()],
        )
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_late_approval_recomputes_a_closed_window(self):
        child = self.create_child()
        item = self.client.post(
            "/api/items/earn",
            json={"name": "迟到项目", "points": 5, "icon": "book.svg", "week_target": 2, "week_bonus": 5},
        ).get_json()
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "child1", "password": "child123"})
        request_id = self.client.post(
            "/api/transactions",
            json={"kind": "earn", "item_id": item["id"], "date": (self.monday(1) + timedelta(days=1)).isoformat()},
        ).get_json()["request_id"]
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})

        self.backdate_rule(item["id"], self.monday(1))
        self.grant(item["id"], weeks_back=1, times=3)
        self.assertEqual(
            [row["amount"] for row in self.settle()["records"] if row["source"] == "weekly_bonus"],
            [5],
        )

        # 审批通过补进一个已定级的窗口并当场重算，发放跟着新计数走。
        self.assertEqual(self.client.post(f"/api/requests/{request_id}/approve", json={}).status_code, 200)
        self.assertEqual(
            [row["amount"] for row in self.settle()["records"] if row["source"] == "weekly_bonus"],
            [10],
        )
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_backdated_log_outside_the_rule_anchor_keeps_paid_history(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.client.post(
            "/api/items/earn",
            json={"name": "改规则项目", "points": 5, "icon": "book.svg", "week_target": 2, "week_bonus": 5},
        ).get_json()
        self.grant(item["id"], weeks_back=1, times=3)
        self.backdate_rule(item["id"], self.monday(1))
        settled = self.settle()
        self.assertEqual([row["amount"] for row in settled["records"] if row["source"] == "weekly_bonus"], [5])

        # 把规则的锚点挪到今天，更早的窗口就不再定级，所以补到那些窗口的迟到记录
        # 只能放着已发的奖金不动，不能清掉一个自己重建不了的窗口。
        self.client.put(
            f"/api/items/earn/{item['id']}",
            json={"name": "改规则项目", "points": 5, "icon": "book.svg", "week_target": 9, "week_bonus": 5},
        )
        added = self.manual_entry(item_kind="earn", item_id=item["id"], date=(self.monday(1) + timedelta(days=3)).isoformat())
        self.assertEqual(added.status_code, 201)
        self.assertEqual([row["amount"] for row in added.get_json()["records"] if row["source"] == "weekly_bonus"], [5])
        self.assertEqual([row["week_start"] for row in self.settlement_rows(item["id"])], [self.monday(1).isoformat()])
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_target_rule_month_window_settles_over_quota(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        app = self.app_module
        current = app.period_start("month", date.today().isoformat(), 1)
        previous = app.period_shift("month", 1, current, -1)
        self.assertEqual(app.period_end("month", previous), (date.fromisoformat(current) - timedelta(days=1)).isoformat())
        item = self.client.post(
            "/api/items/earn",
            json={
                "name": "月度项目",
                "points": 5,
                "icon": "book.svg",
                "week_target": 2,
                "week_bonus": 5,
                "target_unit": "month",
            },
        ).get_json()
        self.assertEqual(item["target_unit"], "month")
        self.backdate_rule(item["id"], date.fromisoformat(previous))
        day = (date.fromisoformat(previous) + timedelta(days=1)).isoformat()
        for _ in range(3):
            self.assertEqual(
                self.client.post("/api/transactions", json={"kind": "earn", "item_id": item["id"], "date": day}).status_code,
                201,
            )
        self.settle("month")
        self.assertEqual([(row["week_start"], row["bonus"]) for row in self.settlement_rows(item["id"])], [(previous, 5)])
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_target_rule_quarter_window_uses_calendar_quarters(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        app = self.app_module
        current = app.period_start("quarter", date.today().isoformat())
        previous = app.period_shift("quarter", 1, current, -1)
        # 季度沿用月的锚点，所以每一格都从 1、4、7、10 月起算。
        self.assertIn(date.fromisoformat(previous).month, {1, 4, 7, 10})
        self.assertEqual(app.period_end("quarter", previous), (date.fromisoformat(current) - timedelta(days=1)).isoformat())
        item = self.client.post(
            "/api/items/earn",
            json={
                "name": "季度项目",
                "points": 5,
                "icon": "book.svg",
                "week_target": 2,
                "week_bonus": 5,
                "target_unit": "quarter",
            },
        ).get_json()
        self.assertEqual(item["target_unit"], "quarter")
        self.backdate_rule(item["id"], date.fromisoformat(previous))
        day = (date.fromisoformat(previous) + timedelta(days=1)).isoformat()
        for _ in range(3):
            self.assertEqual(
                self.client.post("/api/transactions", json={"kind": "earn", "item_id": item["id"], "date": day}).status_code,
                201,
            )
        settled = self.settle("quarter")
        self.assertEqual(
            [(row["title"], row["amount"]) for row in settled["records"] if row["source"] == "weekly_bonus"],
            [("季度项目 3/2", 5)],
        )
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_target_rule_bonus_pays_per_full_bonus_interval(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.client.post(
            "/api/items/earn",
            json={"name": "间隔项目", "points": 5, "icon": "book.svg", "week_target": 1, "week_bonus": 5, "bonus_every": 3},
        ).get_json()
        self.assertEqual(item["bonus_every"], 3)
        self.backdate_rule(item["id"], self.monday(1))
        self.grant(item["id"], weeks_back=1, times=4)
        bonuses = [row for row in self.settle()["records"] if row["source"] == "weekly_bonus"]
        self.assertEqual([(row["title"], row["amount"]) for row in bonuses], [("间隔项目 4/1", 5)])
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_target_rule_validates_unit_and_measure(self):
        rejected = self.client.post(
            "/api/items/earn",
            json={"name": "十年项目", "points": 5, "icon": "book.svg", "week_target": 2, "target_unit": "decade"},
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("目标周期只能选天、周、月、季或年", rejected.get_json()["error"])
        year = self.client.post(
            "/api/items/earn",
            json={"name": "年度项目", "points": 5, "icon": "book.svg", "week_target": 2, "target_unit": "year"},
        ).get_json()
        self.assertEqual(year["target_unit"], "year")
        self.client.delete(f"/api/items/earn/{year['id']}")
        quarter = self.client.post(
            "/api/items/earn",
            json={"name": "季度项目", "points": 5, "icon": "book.svg", "week_target": 2, "target_unit": "quarter"},
        ).get_json()
        self.assertEqual(quarter["target_unit"], "quarter")
        self.client.delete(f"/api/items/earn/{quarter['id']}")
        rejected = self.client.post(
            "/api/items/earn",
            json={"name": "重量项目", "points": 5, "icon": "book.svg", "week_target": 2, "target_measure": "kg"},
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("目标口径只能选次数或数量", rejected.get_json()["error"])
        rejected = self.client.post(
            "/api/items/earn",
            json={"name": "无目标数量", "points": 5, "icon": "book.svg", "target_measure": "quantity"},
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("数量口径需要先设目标", rejected.get_json()["error"])
        item = self.client.post(
            "/api/items/earn",
            json={"name": "默认周期", "points": 5, "icon": "book.svg", "week_target": 2, "target_every": 0, "bonus_every": 0},
        ).get_json()
        self.assertEqual([item["target_unit"], item["target_every"], item["bonus_every"]], ["week", 1, 1])
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_target_rule_year_window_settles_on_calendar_years(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        app = self.app_module
        current = app.period_start("year", date.today().isoformat())
        previous = app.period_shift("year", 1, current, -1)
        # 年份同样沿用月的锚点，所以每一格从 1 月 1 日开到 12 月 31 日。
        self.assertEqual(date.fromisoformat(previous).month, 1)
        self.assertEqual(app.period_end("year", previous), (date.fromisoformat(current) - timedelta(days=1)).isoformat())
        item = self.client.post(
            "/api/items/earn",
            json={
                "name": "年度项目",
                "points": 5,
                "icon": "book.svg",
                "week_target": 2,
                "week_bonus": 5,
                "target_unit": "year",
            },
        ).get_json()
        self.assertEqual(item["target_unit"], "year")
        self.backdate_rule(item["id"], date.fromisoformat(previous))
        day = (date.fromisoformat(previous) + timedelta(days=1)).isoformat()
        for _ in range(4):
            self.assertEqual(
                self.client.post("/api/transactions", json={"kind": "earn", "item_id": item["id"], "date": day}).status_code,
                201,
            )
        # 每条记录只重算自己那个项目，年档窗口已经结过，按周走一遍碰不到它。
        self.assertEqual(self.settle("week")["settlement"]["entries"], 0)
        settled = self.settle("year")
        self.assertEqual(settled["settlement"]["entries"], 0)
        self.assertEqual(
            [(row["title"], row["amount"]) for row in settled["records"] if row["source"] == "weekly_bonus"],
            [("年度项目 4/2", 10)],
        )
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_manual_settlement_walks_forward_from_the_chosen_period(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.client.post(
            "/api/items/earn",
            json={
                "name": "分期项目",
                "points": 5,
                "icon": "book.svg",
                "week_target": 1,
                "week_bonus": 5,
                "streak_weeks": 2,
                "streak_bonus": 20,
            },
        ).get_json()
        self.backdate_rule(item["id"], self.monday(2))
        self.grant(item["id"], weeks_back=2, times=2)
        self.grant(item["id"], weeks_back=1, times=2)

        # 两条记录各自当场重结了自己的窗口，结算从点名的那期起走，也不会重复发钱。
        settled = self.settle("week", self.monday(1).isoformat())
        self.assertEqual(settled["settlement"]["entries"], 0)
        self.assertEqual(
            [(row["title"], row["amount"]) for row in settled["records"] if row["source"] == "weekly_bonus"],
            [("分期项目 2/1", 5), ("分期项目 2/1", 5)],
        )
        self.assertEqual(
            [(row["week_start"], row["bonus"]) for row in self.settlement_rows(item["id"])],
            [(self.monday(2).isoformat(), 5), (self.monday(1).isoformat(), 5)],
        )
        # 连续链统计两个已定级的窗口，所以奖金落在凑满这一串的那一周。
        self.assertEqual(
            [(row["title"], row["amount"]) for row in settled["records"] if row["source"] == "target_streak"],
            [("分期项目 连续 2 期", 20)],
        )
        # 两周都被期次下拉标成已结算，所以按钮要求重新核算而不是再发一遍。
        flags = self.settle_flags()
        self.assertEqual(
            [flags[self.monday(weeks).isoformat()] for weeks in (0, 1, 2)],
            [(False, False), (True, True), (True, True)],
        )

        # 结较早期的窗口一分不发：结算只从自己最新的标记往前推，从不回头。
        settled = self.settle("week", self.monday(2).isoformat())
        self.assertEqual(settled["settlement"]["entries"], 0)
        self.assertEqual(len([row for row in settled["records"] if row["source"] == "weekly_bonus"]), 2)
        self.assertEqual(len([row for row in settled["records"] if row["source"] == "target_streak"]), 1)
        self.assertEqual(self.settle_flags()[self.monday(2).isoformat()], (True, True))
        self.assertEqual(
            [row["week_start"] for row in self.settlement_rows(item["id"])],
            [self.monday(2).isoformat(), self.monday(1).isoformat()],
        )
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_a_backfill_pays_its_window_in_the_same_response(self):
        self.client.post("/api/auth/select-child", json={"child_id": self.create_child()["id"]})
        item = self.client.post(
            "/api/items/earn",
            json={"name": "当场结算", "points": 5, "icon": "book.svg", "week_target": 2, "week_bonus": 4},
        ).get_json()
        self.backdate_rule(item["id"], self.monday(1))
        day = (self.monday(1) + timedelta(days=1)).isoformat()
        window_end = (self.monday(1) + timedelta(days=6)).isoformat()

        for _ in range(2):
            self.client.post("/api/transactions", json={"kind": "earn", "item_id": item["id"], "date": day})
        # 计数项目只达标不发钱，第三条记录超出目标的那笔才进同一条应答。
        answer = self.client.post("/api/transactions", json={"kind": "earn", "item_id": item["id"], "date": day})
        self.assertEqual(
            [
                (row["title"], row["amount"], row["date"])
                for row in answer.get_json()["records"]
                if row["source"] == "weekly_bonus"
            ],
            [("当场结算 3/2", 4, window_end)],
        )
        self.assertEqual(self.settle()["settlement"]["entries"], 0)
        self.assertEqual(
            [(row["week_start"], row["bonus"]) for row in self.settlement_rows(item["id"])],
            [(self.monday(1).isoformat(), 4)],
        )
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_forced_regrade_rewrites_a_paid_period(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.client.post(
            "/api/items/earn",
            json={"name": "重算项目", "points": 5, "icon": "book.svg", "week_target": 1, "week_bonus": 5},
        ).get_json()
        self.backdate_rule(item["id"], self.monday(2))
        self.grant(item["id"], weeks_back=2, times=2)
        self.grant(item["id"], weeks_back=1, times=2)
        self.settle("week", self.monday(1).isoformat())

        rule = {"name": "重算项目", "points": 5, "icon": "book.svg", "week_target": 1, "week_bonus": 9}
        self.client.put(f"/api/items/earn/{item['id']}", json=rule)
        # 改一次规则就把锚点挪到今天，所以用例先把锚点退回更早那期再结算。
        self.backdate_rule(item["id"], self.monday(2))
        # 两周都已被各自记录定过级，只往前推的规则仍然护住它们：新奖金落不进来。
        settled = self.settle("week", self.monday(1).isoformat())
        self.assertEqual(settled["settlement"]["entries"], 0)
        self.assertEqual(
            [row["amount"] for row in settled["records"] if row["source"] == "weekly_bonus"],
            [5, 5],
        )

        # 勾了重新核算会把那期及其之后一起清掉，所以新奖金落在最新那期。
        settled = self.settle("week", self.monday(1).isoformat(), force=True)
        self.assertEqual(
            sorted(row["amount"] for row in settled["records"] if row["source"] == "weekly_bonus"),
            [5, 9],
        )
        self.assertEqual(
            [(row["week_start"], row["bonus"]) for row in self.settlement_rows(item["id"])],
            [(self.monday(2).isoformat(), 5), (self.monday(1).isoformat(), 9)],
        )
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_settlement_controls_validate_unit_and_period(self):
        app = self.app_module
        periods = self.client.get("/api/settlements/periods?unit=week").get_json()["periods"]
        self.assertEqual(len(periods), app.SETTLE_LOOKBACK_PERIODS["week"] + 1)
        # 下拉最老的一格正好压在结算地板上，所以永远不会列出一个没人定级的窗口。
        for unit in ("week", "month", "quarter", "year"):
            listed = self.client.get(f"/api/settlements/periods?unit={unit}").get_json()["periods"]
            floor = app.item_window_floor(
                {"target_unit": unit, "target_every": 1, "rule_set_at": None}, date.today().isoformat()
            )
            self.assertEqual(listed[-1]["start"], floor)
            # 下拉里念的是这一格的首尾日期，不是「某月某日那周」那种要心算的说法。
            for row in listed:
                start, end = (date.fromisoformat(row[key]) for key in ("start", "end"))
                self.assertIn(f"{start.month}-{start.day}~{end.month}-{end.day}", row["label"])
        self.assertEqual([row["start"] for row in periods[:3]], [self.monday(weeks).isoformat() for weeks in (0, 1, 2)])
        self.assertFalse(periods[0]["closed"])
        self.assertTrue(all(row["closed"] for row in periods[1:]))
        self.assertEqual(self.client.get("/api/settlements/periods?unit=day").status_code, 400)
        self.assertEqual(self.client.get("/api/settlements/periods?unit=decade").status_code, 400)
        # 「全部周期」没有可命名的窗口，只给一条补结入口，也不会声称某一期已结算。
        all_periods = self.client.get("/api/settlements/periods?unit=all").get_json()["periods"]
        self.assertEqual([(row["start"], row["closed"], row["settled"]) for row in all_periods], [("", True, False)])

        # 天档随页面结算、没闭合的窗口不能核算、重新核算得先选一期。
        self.assertEqual(self.client.post("/api/settlements/run", json={"unit": "day"}).status_code, 400)
        self.assertEqual(self.client.post("/api/settlements/run", json={"unit": "week", "period": "not-a-date"}).status_code, 400)
        open_week = self.client.post("/api/settlements/run", json={"unit": "week", "period": self.monday(0).isoformat()})
        self.assertEqual(open_week.status_code, 400)
        self.assertIn("尚未结束，不能结算", open_week.get_json()["error"])
        self.assertEqual(self.client.post("/api/settlements/run", json={"unit": "week", "force": True}).status_code, 400)
        self.assertEqual(self.client.post("/api/settlements/run", json={"unit": "all", "period": "2026-01-05"}).status_code, 400)

        self.create_child()
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "child1", "password": "child123"})
        self.assertEqual(self.client.get("/api/settlements/periods?unit=week").status_code, 403)
        self.assertEqual(self.client.post("/api/settlements/run", json={"unit": "week"}).status_code, 403)

    def test_settled_weeks_belong_to_the_child_whose_rule_settled_them(self):
        day_child = self.create_child(username="kid-unit-day", display_name="按天")
        week_child = self.create_child(username="kid-unit-week", display_name="按周")
        item = self.client.post(
            "/api/items/earn",
            json={"name": "共用项目", "points": 2, "icon": "book.svg", "week_target": 1, "week_bonus": 3},
        ).get_json()
        self.client.put(
            f"/api/item-rules/{day_child['id']}",
            json={"kind": "earn", "item_id": item["id"], "week_target": 1, "week_bonus": 3, "target_unit": "day"},
        )
        self.backdate_rule(item["id"], self.monday(2), account_id=day_child["id"])
        self.backdate_rule(item["id"], self.monday(2))
        self.client.post("/api/auth/select-child", json={"child_id": day_child["id"]})
        for _ in range(2):
            self.assertEqual(self.submit_earn(item["id"], date=self.monday(1).isoformat()).status_code, 201)

        with type(self).connection() as conn:
            # 按天那孩子的日结水位正好落在周一，不能因此把按周那孩子的整周标成已结算。
            self.assertIn(self.monday(1).isoformat(), self.app_module.settled_window_starts(conn, "day"))
            self.assertNotIn(self.monday(1).isoformat(), self.app_module.settled_window_starts(conn, "week"))
        self.client.delete(f"/api/items/earn/{item['id']}")

    def streak_records(self, source):
        return [row for row in self.state()["records"] if row["source"] == source]

    def test_windows_of_every_unit_grade_on_their_own_log(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        app = self.app_module
        weekly = self.client.post(
            "/api/items/earn", json={"name": "周结项目", "points": 5, "icon": "book.svg", "week_target": 2, "week_bonus": 3}
        ).get_json()
        month_start = app.period_start("month", date.today().isoformat())
        previous_month = app.period_shift("month", 1, month_start, -1)
        monthly = self.client.post(
            "/api/items/earn",
            json={"name": "月结项目", "points": 5, "icon": "book.svg", "week_target": 2, "week_bonus": 4, "target_unit": "month"},
        ).get_json()
        self.backdate_rule(weekly["id"], self.monday(2))
        self.backdate_rule(monthly["id"], date.fromisoformat(previous_month))
        self.grant(weekly["id"], weeks_back=1, times=3)
        log_day = (date.fromisoformat(previous_month) + timedelta(days=1)).isoformat()
        for _ in range(3):
            self.assertEqual(
                self.client.post("/api/transactions", json={"kind": "earn", "item_id": monthly["id"], "date": log_day}).status_code,
                201,
            )

        # 每个窗口都已按自己的记录结算过，所以「全部周期」这一跑只证明没有重复发放。
        self.assertEqual(self.settle("all")["settlement"]["entries"], 0)
        self.assertEqual(
            [(row["week_start"], row["bonus"]) for row in self.settlement_rows(weekly["id"])],
            [(self.monday(1).isoformat(), 3)],
        )
        self.assertEqual(
            [(row["week_start"], row["bonus"]) for row in self.settlement_rows(monthly["id"])],
            [(previous_month, 4)],
        )
        self.client.delete(f"/api/items/earn/{weekly['id']}")
        self.client.delete(f"/api/items/earn/{monthly['id']}")

    def test_a_new_item_grades_no_windows_from_before_it_existed(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.client.post(
            "/api/items/earn",
            json={"name": "新建项目", "points": 5, "icon": "book.svg", "week_target": 2, "week_penalty": 3},
        ).get_json()
        self.assertIsNotNone(item["rule_set_at"])
        self.grant(item["id"], weeks_back=1, times=2)

        # 规则今天才起算，上周补进来的记录属于没有规则的历史，不算未达标。
        self.assertEqual(self.settle("week", period=self.monday(1).isoformat())["settlement"]["entries"], 0)
        self.assertEqual(self.settlement_rows(item["id"]), [])
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_a_rule_set_mid_window_skips_its_own_incomplete_window(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.client.post(
            "/api/items/earn",
            json={"name": "周中设立", "points": 5, "icon": "book.svg", "week_target": 2, "week_bonus": 3, "week_penalty": 3},
        ).get_json()
        self.grant(item["id"], weeks_back=2, times=1)
        self.grant(item["id"], weeks_back=1, times=3)
        self.backdate_rule(item["id"], self.monday(2) + timedelta(days=2))

        # 规则定在半途的那一周既不发奖也不罚分，下一整个星期才开始定级。
        self.settle("all")
        self.assertEqual(
            [(row["week_start"], row["bonus"], row["penalty"]) for row in self.settlement_rows(item["id"])],
            [(self.monday(1).isoformat(), 3, 0)],
        )
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_deleting_an_item_keeps_the_windows_its_logs_graded(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        kept = self.client.post(
            "/api/items/earn", json={"name": "保留项目", "points": 5, "icon": "book.svg", "week_target": 2, "week_bonus": 3}
        ).get_json()
        gone = self.client.post(
            "/api/items/earn", json={"name": "删除项目", "points": 5, "icon": "book.svg", "week_target": 2, "week_bonus": 3}
        ).get_json()
        self.backdate_rule(kept["id"], self.monday(2))
        self.backdate_rule(gone["id"], self.monday(2))
        self.grant(kept["id"], weeks_back=1, times=3)
        self.grant(gone["id"], weeks_back=1, times=3)
        self.client.delete(f"/api/items/earn/{gone['id']}")

        # 两个项目都在各自记录落账时结清了，再走一遍无可发。
        self.assertEqual(self.settle("week", period=self.monday(1).isoformat())["settlement"]["entries"], 0)
        self.assertEqual(
            [(row["week_start"], row["bonus"]) for row in self.settlement_rows(kept["id"])],
            [(self.monday(1).isoformat(), 3)],
        )
        # 删除项目不动它已定级的窗口：记录已经发出去的积分仍然算数。
        self.assertEqual(
            [(row["week_start"], row["bonus"]) for row in self.settlement_rows(gone["id"])],
            [(self.monday(1).isoformat(), 3)],
        )
        with type(self).connection() as conn:
            logs = conn.execute(
                "SELECT COUNT(*) FROM records WHERE item_kind = 'earn' AND item_id = ? AND source = 'earn'",
                (gone["id"],),
            ).fetchone()[0]
        self.assertEqual(logs, 3)
        self.client.delete(f"/api/items/earn/{kept['id']}")

    def test_target_streak_pays_after_weeks_on_target(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.client.post(
            "/api/items/earn",
            json={
                "name": "连续项目",
                "points": 1,
                "icon": "book.svg",
                "week_target": 1,
                "streak_weeks": 2,
                "streak_bonus": 20,
            },
        ).get_json()
        self.backdate_rule(item["id"], self.monday(2))
        self.grant(item["id"], weeks_back=2)
        self.grant(item["id"], weeks_back=1)

        self.settle()
        streaks = self.streak_records("target_streak")
        self.assertEqual([row["title"] for row in streaks], ["连续项目 连续 2 期"])
        self.assertEqual(streaks[0]["amount"], 20)
        self.assertEqual(streaks[0]["date"], (self.monday(1) + timedelta(days=6)).isoformat())
        self.assertEqual(len(self.streak_records("target_streak")), 1)
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_target_streak_pays_every_full_daily_run(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.client.post(
            "/api/items/earn",
            json={
                "name": "每日连续",
                "points": 2,
                "icon": "book.svg",
                "target_unit": "day",
                "week_target": 1,
                "streak_weeks": 3,
                "streak_bonus": 6,
            },
        ).get_json()
        self.assertEqual(item["target_unit"], "day")
        self.backdate_rule(item["id"], date.today() - timedelta(days=5))
        for days_back in (4, 3, 2, 1):
            day = (date.today() - timedelta(days=days_back)).isoformat()
            self.assertEqual(self.submit_earn(item["id"], date=day).status_code, 201)

        # 连着四天只在第三天发一次，第四天另起一串。
        self.assertEqual(
            [(row["title"], row["amount"], row["date"]) for row in self.streak_records("target_streak")],
            [("每日连续 连续 3 期", 6, (date.today() - timedelta(days=2)).isoformat())],
        )
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_target_streak_resets_after_a_missed_window(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.client.post(
            "/api/items/earn",
            json={
                "name": "隔天项目",
                "points": 1,
                "icon": "book.svg",
                "target_unit": "day",
                "week_target": 1,
                "streak_weeks": 2,
                "streak_bonus": 4,
            },
        ).get_json()
        self.backdate_rule(item["id"], date.today() - timedelta(days=5))
        for days_back in (1, 3):
            day = (date.today() - timedelta(days=days_back)).isoformat()
            self.assertEqual(self.submit_earn(item["id"], date=day).status_code, 201)
        self.assertEqual(self.streak_records("target_streak"), [])

        self.assertEqual(self.submit_earn(item["id"], date=(date.today() - timedelta(days=2)).isoformat()).status_code, 201)
        payouts = self.streak_records("target_streak")
        self.assertEqual([row["date"] for row in payouts], [(date.today() - timedelta(days=2)).isoformat()])
        self.assertEqual(payouts[0]["amount"], 4)
        self.assertEqual(len(self.streak_records("target_streak")), 1)
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_target_streak_keeps_its_cadence_past_the_lookback(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.client.post(
            "/api/items/earn",
            json={
                "name": "长链项目",
                "points": 1,
                "icon": "book.svg",
                "week_target": 1,
                "streak_weeks": 5,
                "streak_bonus": 2,
            },
        ).get_json()
        # 链长 20 周，比 12 周的回看长度还长：相位必须从规则起点数起。
        self.backdate_rule(item["id"], self.monday(20))
        for weeks_back in range(20, 0, -1):
            self.grant(item["id"], weeks_back=weeks_back)
        self.settle()

        # 每个整 5 期的末尾发一次；回看只决定从哪一期开始写，不改动相位。
        self.assertEqual(
            sorted(row["date"] for row in self.streak_records("target_streak")),
            [(self.monday(weeks) + timedelta(days=6)).isoformat() for weeks in (11, 6, 1)],
        )
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_deduct_streak_charges_extra_penalty(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.client.post(
            "/api/items/deduct",
            json={"name": "连续迟到", "points": 2, "icon": "warning.svg", "streak_weeks": 2, "streak_bonus": 5},
        ).get_json()
        self.assertEqual((item["streak_weeks"], item["streak_bonus"], item["streak_unit"]), (2, 5, "week"))

        invalid = self.client.put(
            f"/api/items/deduct/{item['id']}",
            json={"name": "连续迟到", "points": 2, "icon": "warning.svg", "streak_bonus": 5},
        )
        self.assertEqual(invalid.status_code, 400)
        bad_unit = self.client.put(
            f"/api/items/deduct/{item['id']}",
            json={
                "name": "连续迟到",
                "points": 2,
                "icon": "warning.svg",
                "streak_weeks": 2,
                "streak_bonus": 5,
                "streak_unit": "decade",
            },
        )
        self.assertEqual(bad_unit.status_code, 400)
        self.assertIn("连续周期只能选天、周、月、季或年", bad_unit.get_json()["error"])

        self.backdate_rule(item["id"], self.monday(2), kind="deduct")
        for weeks_back in (2, 1):
            day = self.monday(weeks_back) + timedelta(days=1)
            response = self.client.post(
                "/api/transactions", json={"kind": "deduct", "item_id": item["id"], "date": day.isoformat()}
            )
            self.assertEqual(response.status_code, 201)

        self.settle()
        payouts = self.streak_records("deduct_week_streak")
        self.assertEqual([(row["title"], row["amount"], row["type"]) for row in payouts], [("连续迟到 连续 2 周", -5, "expense")])
        data = self.state()
        self.assertEqual(data["total_points"], -9)
        self.assertEqual(len(self.streak_records("deduct_week_streak")), 1)

        updated = self.client.put(
            f"/api/items/deduct/{item['id']}",
            json={"name": "连续迟到", "points": 2, "icon": "warning.svg", "streak_weeks": 3, "streak_bonus": 5},
        ).get_json()
        self.assertEqual((updated["streak_weeks"], updated["streak_unit"]), (3, "week"))
        self.client.delete(f"/api/items/deduct/{item['id']}")

    def test_deduct_streak_can_charge_per_month(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        app = self.app_module
        item = self.client.post(
            "/api/items/deduct",
            json={
                "name": "月度迟到",
                "points": 1,
                "icon": "warning.svg",
                "streak_weeks": 2,
                "streak_bonus": 5,
                "streak_unit": "month",
            },
        ).get_json()
        self.assertEqual(item["streak_unit"], "month")
        this_month = app.period_start("month", date.today().isoformat())
        first = app.period_shift("month", 1, this_month, -2)
        self.backdate_rule(item["id"], date.fromisoformat(first), kind="deduct")
        for months_back in (2, 1):
            day = date.fromisoformat(app.period_shift("month", 1, this_month, -months_back)) + timedelta(days=1)
            self.deduct_on(item["id"], day)
        self.settle("month")
        payouts = self.streak_records("deduct_month_streak")
        self.assertEqual([(row["title"], row["amount"], row["type"]) for row in payouts], [("月度迟到 连续 2 个月", -5, "expense")])
        self.assertEqual(payouts[0]["date"], app.period_end("month", app.period_shift("month", 1, this_month, -1)))
        self.client.delete(f"/api/items/deduct/{item['id']}")

    def test_deduct_streak_keeps_its_cadence_past_the_lookback(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.client.post(
            "/api/items/deduct",
            json={"name": "长链迟到", "points": 1, "icon": "warning.svg", "streak_weeks": 5, "streak_bonus": 3},
        ).get_json()
        # 连续 20 周都迟到，链长超过 12 周的回看长度：扣分要照整条链的相位来。
        self.backdate_rule(item["id"], self.monday(20), kind="deduct")
        for weeks_back in range(20, 0, -1):
            self.deduct_on(item["id"], self.monday(weeks_back) + timedelta(days=1))
        self.settle()

        # 每满 5 周扣一次，回看之前的整段只参与计数、不补发。
        self.assertEqual(
            sorted(row["date"] for row in self.streak_records("deduct_week_streak")),
            [(self.monday(weeks) + timedelta(days=6)).isoformat() for weeks in (11, 6, 1)],
        )
        self.client.delete(f"/api/items/deduct/{item['id']}")

    def test_deduct_target_only_penalizes_above_the_cap(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.client.post(
            "/api/items/deduct",
            json={
                "name": "看电视",
                "points": 2,
                "icon": "warning.svg",
                "week_target": 2,
                "week_bonus": 3,
                "target_measure": "quantity",
            },
        ).get_json()
        # 扣分项目的目标按提交次数计：不取数量口径，也就不存在按缺口补发的那一层。
        self.assertEqual(item["week_penalty"], 0)
        self.assertEqual(item["target_measure"], "count")
        self.assertEqual(item["count_days_once"], 0)
        self.backdate_rule(item["id"], self.monday(2), kind="deduct")

        self.deduct_on(item["id"], self.monday(1) + timedelta(days=1), times=2)
        self.assertEqual(self.settle()["total_points"], -4)
        self.assertEqual(self.settlement_rows(item["id"], "deduct"), [])

        self.deduct_on(item["id"], self.monday(1) + timedelta(days=2))
        settled = self.settle()
        charges = [row for row in settled["records"] if row["source"] == "over_cap_penalty"]
        self.assertEqual(
            [(row["title"], row["amount"], row["type"], row["date"]) for row in charges],
            [("看电视 3/2", -3, "expense", (self.monday(1) + timedelta(days=6)).isoformat())],
        )
        self.assertEqual(
            [(row["week_start"], row["bonus"], row["penalty"]) for row in self.settlement_rows(item["id"], "deduct")],
            [(self.monday(1).isoformat(), 0, 3)],
        )
        self.assertEqual(settled["total_points"], -9)

        # 已定级的窗口里再补一条会当场重算，扣的分跟着新计数走。
        self.deduct_on(item["id"], self.monday(1) + timedelta(days=3))
        settled = self.settle()
        charges = [row for row in settled["records"] if row["source"] == "over_cap_penalty"]
        self.assertEqual(
            [(row["title"], row["amount"]) for row in charges],
            [("看电视 4/2", -6)],
        )
        self.assertEqual(settled["total_points"], -14)
        # 同一期仍然可以重新核算，结果也落在同一个答案上。
        settled = self.settle("week", self.monday(1).isoformat(), force=True)
        charges = [row for row in settled["records"] if row["source"] == "over_cap_penalty"]
        self.assertEqual(
            [(row["title"], row["amount"]) for row in charges],
            [("看电视 4/2", -6)],
        )
        self.assertEqual(settled["total_points"], -14)
        self.client.delete(f"/api/items/deduct/{item['id']}")

    def test_streak_payouts_refuse_individual_undo(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        earn = self.client.post(
            "/api/items/earn",
            json={
                "name": "每日连续",
                "points": 2,
                "icon": "book.svg",
                "target_unit": "day",
                "week_target": 1,
                "streak_weeks": 2,
                "streak_bonus": 4,
            },
        ).get_json()
        self.backdate_rule(earn["id"], date.today() - timedelta(days=4))
        for days_back in (3, 2, 1):
            self.assertEqual(self.submit_earn(earn["id"], date=(date.today() - timedelta(days=days_back)).isoformat()).status_code, 201)

        # 天档随页面加载结算，所以连续记录一旦被删，同一次撤销请求里就会被补发回来。
        payouts = self.streak_records("target_streak")
        self.assertEqual([row["amount"] for row in payouts], [4])
        refused = self.client.post(f"/api/transactions/{payouts[0]['id']}/undo")
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.get_json()["error"], "连续奖惩记录不能单独撤销，请用首页的重新核算调整")
        self.assertEqual([row["id"] for row in self.streak_records("target_streak")], [row["id"] for row in payouts])

        deduct = self.client.post(
            "/api/items/deduct",
            json={
                "name": "天天迟到",
                "points": 1,
                "icon": "warning.svg",
                "streak_weeks": 2,
                "streak_bonus": 3,
                "streak_unit": "day",
            },
        ).get_json()
        self.backdate_rule(deduct["id"], date.today() - timedelta(days=4), kind="deduct")
        for days_back in (3, 2, 1):
            self.deduct_on(deduct["id"], date.today() - timedelta(days=days_back))
        charges = self.streak_records("deduct_day_streak")
        self.assertEqual([row["amount"] for row in charges], [-3])
        self.assertEqual(self.client.post(f"/api/transactions/{charges[0]['id']}/undo").status_code, 409)
        self.assertEqual(len(self.streak_records("deduct_day_streak")), 1)

        self.client.delete(f"/api/items/earn/{earn['id']}")
        self.client.delete(f"/api/items/deduct/{deduct['id']}")

    def test_windowed_settlement_can_still_be_undone(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.client.post(
            "/api/items/earn",
            json={"name": "阅读", "points": 5, "icon": "book.svg", "week_target": 2, "week_bonus": 4},
        ).get_json()
        self.backdate_rule(item["id"], self.monday(1))
        self.grant(item["id"], weeks_back=1, times=3)
        bonuses = [row for row in self.settle()["records"] if row["source"] == "weekly_bonus"]
        self.assertEqual([(row["title"], row["amount"]) for row in bonuses], [("阅读 3/2", 4)])

        # 那一期的水位行还在，所以撤销掉的发放不会被下一次结算补发。
        self.assertEqual(self.client.post(f"/api/transactions/{bonuses[0]['id']}/undo").status_code, 200)
        self.assertEqual(len(self.settlement_rows(item["id"])), 1)
        settled = self.settle("all")
        self.assertEqual([row for row in settled["records"] if row["source"] == "weekly_bonus"], [])
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_unbinding_a_project_keeps_the_points_it_already_paid(self):
        child = self.create_child(username="kid-unbind")
        item = self.client.post(
            "/api/items/earn",
            json={"name": "解绑项目", "points": 5, "icon": "book.svg", "week_target": 2, "week_bonus": 4},
        ).get_json()
        self.backdate_rule(item["id"], self.monday(1))
        self.grant(item["id"], weeks_back=1, times=3)
        bonuses = [row for row in self.settle()["records"] if row["source"] == "weekly_bonus"]
        self.assertEqual([row["amount"] for row in bonuses], [4])

        # 解绑后项目退出结算集合；此后撤销一条记录只该撤它自己，不能把已发的奖励一起删掉。
        self.assertEqual(
            self.client.put(
                f"/api/item-bindings/{child['id']}",
                json={"bindings": [{"kind": "earn", "item_id": item["id"], "enabled": False}]},
            ).status_code,
            200,
        )
        logs = [row for row in self.state()["records"] if row["item_id"] == item["id"] and row["source"] == "earn"]
        self.assertEqual(self.client.post(f"/api/transactions/{logs[0]['id']}/undo").status_code, 200)

        kept = [row for row in self.state()["records"] if row["source"] == "weekly_bonus"]
        self.assertEqual([row["amount"] for row in kept], [4])
        self.assertEqual(self.state()["total_points"], 2 * 5 + 4)
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_earn_streak_award_and_fine_coexist(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.client.post(
            "/api/items/earn",
            json={
                "name": "连续缺练",
                "points": 5,
                "icon": "book.svg",
                "week_target": 2,
                "streak_weeks": 3,
                "streak_bonus": 10,
                "streak_penalty_weeks": 2,
                "streak_penalty": 5,
            },
        ).get_json()
        self.assertEqual(
            (item["streak_weeks"], item["streak_bonus"], item["streak_penalty_weeks"], item["streak_penalty"]),
            (3, 10, 2, 5),
        )
        self.assertEqual(
            self.client.post(
                "/api/items/earn",
                json={"name": "缺目标", "points": 5, "icon": "book.svg", "streak_penalty_weeks": 2, "streak_penalty": 5},
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                "/api/items/earn",
                json={"name": "缺期数", "points": 5, "icon": "book.svg", "week_target": 2, "streak_penalty": 5},
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                "/api/items/earn",
                json={"name": "负值奖励", "points": 5, "icon": "book.svg", "week_target": 2, "streak_bonus": -5},
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                "/api/items/deduct",
                json={"name": "负值扣分", "points": 2, "icon": "warning.svg", "streak_weeks": 2, "streak_bonus": -5},
            ).status_code,
            400,
        )

        self.backdate_rule(item["id"], self.monday(2))
        # 前两周各只给一次，两次都没到每周两次的目标，所以只有罚分链在跑。
        self.grant(item["id"], weeks_back=2)
        self.grant(item["id"], weeks_back=1)
        self.settle()
        self.assertEqual(
            [(row["title"], row["amount"], row["type"]) for row in self.streak_records("target_streak_penalty")],
            [("连续缺练 连续 2 期", -5, "expense")],
        )
        self.assertEqual(self.streak_records("target_streak"), [])

    def test_upgrade_moves_a_negative_streak_bonus_into_the_fine(self):
        item_id = self.client.post(
            "/api/items/earn",
            json={"name": "老规则", "points": 5, "icon": "book.svg", "week_target": 2, "streak_weeks": 2, "streak_bonus": 5},
        ).get_json()["id"]
        with type(self).connection() as conn:
            # 罚分单列之前的老安装把它存成负数加分，升级时要搬进罚分列。
            conn.execute("UPDATE earn_items SET streak_bonus = -5 WHERE id = ?", (item_id,))
            self.app_module.ensure_item_schema(conn)
            row = conn.execute(
                "SELECT streak_weeks, streak_bonus, streak_penalty_weeks, streak_penalty FROM earn_items WHERE id = ?",
                (item_id,),
            ).fetchone()
        self.assertEqual(tuple(row), (2, 0, 2, 5))
        self.client.delete(f"/api/items/earn/{item_id}")

    def test_upgrade_drops_the_item_switch_columns(self):
        item_id = self.client.post(
            "/api/items/earn", json={"name": "重新出现", "points": 3, "icon": "book.svg"}
        ).get_json()["id"]
        with type(self).connection() as conn:
            # 项目带开关列那阵子的老安装把开关存在每个项目上，删掉这列就等于把藏起来的项目放回来。
            conn.execute("ALTER TABLE earn_items ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
            conn.execute("ALTER TABLE earn_items ADD COLUMN active_from TEXT")
            conn.execute("UPDATE earn_items SET active = 0 WHERE id = ?", (item_id,))
            self.app_module.ensure_item_schema(conn)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(earn_items)").fetchall()}
        self.assertNotIn("active", columns)
        self.assertNotIn("active_from", columns)
        self.assertIn(item_id, [row["id"] for row in self.state()["item_library"]["earn"]])
        self.client.delete(f"/api/items/earn/{item_id}")

    def test_weekly_rule_validation(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.client.post(
            "/api/items/earn", json={"name": "验证项目", "points": 8, "icon": "book.svg"}
        ).get_json()

        response = self.client.put(
            f"/api/items/earn/{item['id']}",
            json={"name": "验证项目", "points": 8, "icon": "book.svg", "week_target": 3, "week_bonus": 2},
        )
        self.assertEqual(response.get_json()["week_target"], 3)
        response = self.client.put(
            f"/api/items/earn/{item['id']}",
            json={"name": "验证项目", "points": 8, "icon": "book.svg", "week_target": 3, "streak_bonus": 5},
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.put(
            f"/api/items/earn/{item['id']}",
            json={"name": "验证项目", "points": 8, "icon": "book.svg", "week_target": -1},
        )
        self.assertEqual(response.status_code, 400)
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_count_days_once_tallies_distinct_days(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.client.post(
            "/api/items/earn",
            json={"name": "按天统计", "points": 5, "icon": "book.svg", "week_target": 2, "week_bonus": 3, "count_days_once": 1},
        ).get_json()
        self.assertEqual(item["count_days_once"], 1)
        self.backdate_rule(item["id"], self.monday(1))
        self.grant(item["id"], weeks_back=1, times=3)
        data = self.state()
        self.assertEqual([row for row in data["records"] if row["source"] == "weekly_bonus"], [])
        self.assertEqual(data["total_points"], 15)

        self.grant(item["id"], weeks_back=0, times=2)
        row = self.earn_item_row(item["id"])
        self.assertEqual(row["day_counts"][date.today().isoformat()], 2)
        self.assertEqual(row["week_count"], 1)
        self.client.delete(f"/api/items/earn/{item['id']}")

    def submit_earn(self, item_id, **payload):
        return self.client.post("/api/transactions", json={"kind": "earn", "item_id": item_id, **payload})

    def quantity_item(self, name, points=1, **rule):
        response = self.client.post(
            "/api/items/earn", json={"name": name, "points": points, "icon": "book.svg", **rule}
        )
        self.assertEqual(response.status_code, 201)
        return response.get_json()

    def earn_logs(self):
        return [row for row in self.state()["records"] if row["source"] == "earn"]

    def test_quantity_item_settles_from_the_whole_window_total(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.quantity_item(
            "计算题",
            points=3,
            week_target=10,
            week_bonus=2,
            bonus_every=5,
            target_measure="quantity",
            count_days_once=1,
        )
        self.assertEqual((item["target_measure"], item["count_days_once"]), ("quantity", 0))

        missing = self.submit_earn(item["id"])
        self.assertEqual(missing.status_code, 400)
        self.assertIn("数量必须大于 0", missing.get_json()["error"])
        self.assertEqual(self.submit_earn(item["id"], quantity=0).status_code, 400)

        self.backdate_rule(item["id"], self.monday(1))
        self.assertEqual(self.submit_earn(item["id"], date=(self.monday(1) + timedelta(days=1)).isoformat(), quantity=7).status_code, 201)
        # 没到 10 个的目标就一分不发，那一整周因此留着等后来的记录。
        self.assertEqual([row for row in self.settle()["records"] if row["source"] == "weekly_bonus"], [])
        self.assertEqual(self.submit_earn(item["id"], date=(self.monday(1) + timedelta(days=3)).isoformat(), quantity=20).status_code, 201)

        # 录入本身零分；27 个结出 3 分完成分，另按超出目标的 17 个每满 5 个加 2 分。
        self.assertEqual([(row["amount"], row["quantity"]) for row in self.earn_logs()], [(0, 20), (0, 7)])
        bonuses = [row for row in self.settle()["records"] if row["source"] == "weekly_bonus"]
        self.assertEqual([(row["title"], row["amount"]) for row in bonuses], [("计算题 27/10", 9)])
        self.assertEqual(self.state()["total_points"], 9)
        self.assertEqual(len(self.state()["records"]), len(bonuses) + 2)

        # 零分记录照样算完成，但只有结算出来的奖金才带积分。
        periods = self.client.get("/api/analytics?days=30").get_json()["item_periods"]
        earned = next(row for row in periods["items"]["earn"] if row["name"] == "计算题")
        cell = next(cell for cell in earned["cells"] if cell and cell["detail"])
        self.assertEqual((cell["measure"], earned["total"]["count"], earned["total"]["quantity"]), ("quantity", 2, 27))
        self.assertEqual((earned["total"]["coins"], earned["total"]["payout"]), (0, 9))
        # 奖惩分列是合计，下面的明细就是它背后的结算记录。
        self.assertEqual(
            [(row["title"], row["source"], row["amount"]) for row in cell["detail"]],
            [("计算题 27/10", "weekly_bonus", 9)],
        )
        self.assertEqual([row["name"] for row in periods["items"]["deduct"] if row["name"] == "计算题"], [])
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_quantity_completion_pays_plus_only_the_full_overshoot(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.quantity_item(
            "英语单词背诵", points=2, week_target=2, week_bonus=1, bonus_every=2, target_measure="quantity"
        )
        self.backdate_rule(item["id"], self.monday(2))
        for weeks_back, quantity in ((2, 3), (1, 6)):
            day = (self.monday(weeks_back) + timedelta(days=1)).isoformat()
            self.assertEqual(self.submit_earn(item["id"], date=day, quantity=quantity).status_code, 201)

        # 每个闭合窗口发 2 分完成分，只有超出目标的数量才计分，所以 3 个不加、6 个加 2。
        bonuses = [row for row in self.settle()["records"] if row["source"] == "weekly_bonus"]
        self.assertEqual(
            [(row["title"], row["amount"]) for row in bonuses],
            [("英语单词背诵 6/2", 4), ("英语单词背诵 3/2", 2)],
        )
        self.assertEqual(self.state()["total_points"], 6)
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_quantity_item_penalizes_a_short_window(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.quantity_item("练字", week_target=10, week_penalty=5, target_measure="quantity")
        self.backdate_rule(item["id"], self.monday(1))
        day = (self.monday(1) + timedelta(days=2)).isoformat()
        self.assertEqual(self.submit_earn(item["id"], date=day, quantity=3).status_code, 201)

        penalties = [row for row in self.settle()["records"] if row["source"] == "weekly_penalty"]
        self.assertEqual([(row["title"], row["amount"]) for row in penalties], [("练字 3/10", -5)])

        day = date.today().isoformat()
        self.assertEqual(self.submit_earn(item["id"], date=day, quantity=4).status_code, 201)
        row = self.earn_item_row(item["id"])
        self.assertEqual((row["day_counts"][day], row["week_count"]), (4, 4))
        self.assertEqual(self.state()["total_points"], -5)
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_quantity_streak_counts_windows_on_cumulative_quantity(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.quantity_item(
            "口算本", week_target=10, target_measure="quantity", streak_weeks=2, streak_bonus=6
        )
        self.backdate_rule(item["id"], self.monday(2))
        for weeks_back, quantity in ((2, 12), (1, 10)):
            day = (self.monday(weeks_back) + timedelta(days=1)).isoformat()
            self.assertEqual(self.submit_earn(item["id"], date=day, quantity=quantity).status_code, 201)

        self.settle()
        payouts = self.streak_records("target_streak")
        self.assertEqual([(row["title"], row["amount"]) for row in payouts], [("口算本 连续 2 期", 6)])
        self.assertEqual(payouts[0]["date"], (self.monday(1) + timedelta(days=6)).isoformat())
        self.assertEqual(len(self.streak_records("target_streak")), 1)
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_quantity_requests_keep_the_logged_quantity(self):
        child = self.create_child()
        item = self.quantity_item("课外阅读", week_target=20, target_measure="quantity")
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "child1", "password": "child123"})
        response = self.submit_earn(item["id"], quantity=15)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.submit_earn(item["id"]).status_code, 400)

        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        self.assertEqual(self.client.post(f"/api/requests/{response.get_json()['request_id']}/approve").status_code, 200)
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        self.assertEqual([(row["amount"], row["quantity"]) for row in self.earn_logs()], [(0, 15)])
        self.client.delete(f"/api/items/earn/{item['id']}")

    def manual_entry(self, **payload):
        return self.client.post("/api/transactions", json={"kind": "manual", **payload})

    def manual_logs(self, payload):
        return [row for row in payload["records"] if row["source"] == "manual"]

    def test_manual_entry_uses_the_selected_item_rules(self):
        self.client.post("/api/auth/select-child", json={"child_id": self.create_child()["id"]})
        earn = self.client.post("/api/items/earn", json={"name": "补录加分", "points": 5, "icon": "book.svg"}).get_json()
        deduct = self.client.post("/api/items/deduct", json={"name": "补录扣分", "points": 4, "icon": "warning.svg"}).get_json()
        day = date.today().isoformat()

        missing = self.manual_entry(item_kind="earn", name="线下奖励", date=day)
        self.assertEqual(missing.status_code, 400)
        self.assertIn("请选择补录项目", missing.get_json()["error"])

        added = self.manual_entry(item_kind="earn", item_id=earn["id"], name="线下奖励", date=day)
        self.assertEqual(added.status_code, 201)
        row = self.manual_logs(added.get_json())[0]
        self.assertEqual((row["title"], row["amount"], row["date"]), ("补录：补录加分（线下奖励）", 5, day))

        removed = self.manual_entry(item_kind="deduct", item_id=deduct["id"], date=day)
        self.assertEqual(removed.status_code, 201)
        row = self.manual_logs(removed.get_json())[0]
        self.assertEqual((row["title"], row["amount"]), ("补录：补录扣分", -4))
        self.assertEqual(removed.get_json()["total_points"], 1)

        # 不选项目时，手填的分照样生效。
        free = self.manual_entry(name="线下补偿", points=8, date=day)
        self.assertEqual(self.manual_logs(free.get_json())[0]["title"], "补积分：线下补偿")
        # 记录写的是已经发生的事，所以晚于今天的日期会被拒。
        future = self.manual_entry(item_kind="earn", item_id=earn["id"], date=(date.today() + timedelta(days=1)).isoformat())
        self.assertEqual(future.status_code, 400)
        self.assertIn("发生日期不能晚于今天", future.get_json()["error"])
        self.client.delete(f"/api/items/earn/{earn['id']}")
        self.client.delete(f"/api/items/deduct/{deduct['id']}")

    def test_manual_entry_of_a_quantity_item_logs_quantity(self):
        self.client.post("/api/auth/select-child", json={"child_id": self.create_child()["id"]})
        item = self.quantity_item(
            "错题整理",
            points=3,
            week_target=10,
            week_bonus=2,
            bonus_every=5,
            target_measure="quantity",
        )
        self.backdate_rule(item["id"], self.monday(1))
        day = (self.monday(1) + timedelta(days=2)).isoformat()

        self.assertEqual(self.manual_entry(item_kind="earn", item_id=item["id"], date=day).status_code, 400)
        response = self.manual_entry(item_kind="earn", item_id=item["id"], quantity=12, date=day)
        self.assertEqual(response.status_code, 201)
        # 补录本身不带积分；闭合窗口发出 3 分完成奖励，超出目标 2 个还够不上每 5 个一档。
        self.assertEqual(
            [(row["title"], row["amount"], row["quantity"]) for row in self.manual_logs(response.get_json())],
            [("补录：错题整理", 0, 12)],
        )
        settled = self.settle()
        bonuses = [row for row in settled["records"] if row["source"] == "weekly_bonus"]
        self.assertEqual([(row["title"], row["amount"]) for row in bonuses], [("错题整理 12/10", 3)])
        self.assertEqual(settled["total_points"], 3)
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_manual_backdating_regrades_its_own_window(self):
        self.client.post("/api/auth/select-child", json={"child_id": self.create_child()["id"]})
        item = self.client.post(
            "/api/items/earn",
            json={"name": "阅读", "points": 5, "icon": "book.svg", "week_target": 2, "week_penalty": 3, "week_bonus": 4},
        ).get_json()
        self.backdate_rule(item["id"], self.monday(1))
        self.grant(item["id"], weeks_back=1)
        # 记录落账时就重算了自己那个闭合窗口，所以这一次未达标已经罚过了。
        settled = self.settle()
        self.assertEqual(settled["settlement"]["entries"], 0)
        self.assertEqual(
            [(row["title"], row["amount"]) for row in settled["records"] if row["source"] == "weekly_penalty"],
            [("阅读 1/2", -3)],
        )
        self.assertEqual(settled["total_points"], 2)
        self.assertEqual(self.watermarks(item["id"]), [(self.monday(1).isoformat(), 0, 3)])

        # 补录会改写它落入的那个窗口：达标之后，已经发过的罚款被收回。
        added = self.manual_entry(
            item_kind="earn", item_id=item["id"], date=(self.monday(1) + timedelta(days=3)).isoformat()
        )
        self.assertEqual(added.status_code, 201)
        settled = self.settle()
        self.assertEqual([row for row in settled["records"] if row["source"].startswith("weekly")], [])
        self.assertEqual(self.watermarks(item["id"]), [])
        self.assertEqual(settled["total_points"], 10)

        # 撤销这笔补录后计数又跌回目标以下，罚款随之回来。
        undone = self.client.post(f"/api/transactions/{added.get_json()['record_id']}/undo")
        self.assertEqual(undone.status_code, 200)
        self.assertEqual([row["penalty"] for row in self.settlement_rows(item["id"])], [3])
        self.assertEqual(self.state()["total_points"], 2)

        # 再补一笔又会自己清掉罚款，所以重新核算只是重复同一个答案。
        self.assertEqual(
            self.manual_entry(
                item_kind="earn", item_id=item["id"], date=(self.monday(1) + timedelta(days=4)).isoformat()
            ).status_code,
            201,
        )
        settled = self.settle("week", self.monday(1).isoformat(), force=True)
        self.assertEqual([row for row in settled["records"] if row["source"].startswith("weekly")], [])
        self.assertEqual(self.settlement_rows(item["id"]), [])
        self.assertEqual(settled["total_points"], 10)
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_manual_deduct_entry_fills_the_ceiling_window(self):
        self.client.post("/api/auth/select-child", json={"child_id": self.create_child()["id"]})
        item = self.client.post(
            "/api/items/deduct",
            json={"name": "发脾气", "points": 4, "icon": "warning.svg", "week_target": 1, "week_bonus": 2},
        ).get_json()
        self.backdate_rule(item["id"], self.monday(1), kind="deduct")
        day = (self.monday(1) + timedelta(days=1)).isoformat()

        first = self.manual_entry(item_kind="deduct", item_id=item["id"], date=day)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(self.settlement_rows(item["id"], "deduct"), [])

        second = self.manual_entry(item_kind="deduct", item_id=item["id"], date=day)
        self.assertEqual(second.status_code, 201)
        settled = self.settle()
        charges = [row for row in settled["records"] if row["source"] == "over_cap_penalty"]
        self.assertEqual([(row["title"], row["amount"]) for row in charges], [("发脾气 2/1", -2)])
        self.assertEqual(settled["total_points"], -10)
        self.client.delete(f"/api/items/deduct/{item['id']}")

    def test_cap_period_limits_daily_and_weekly_submissions(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        daily = self.client.post(
            "/api/items/earn", json={"name": "每日上限", "points": 3, "icon": "book.svg", "cap_period": "day"}
        ).get_json()
        self.assertEqual(daily["cap_period"], "day")
        self.assertEqual(self.earn_item_row(daily["id"])["cap_period"], "day")

        self.grant(daily["id"], weeks_back=0)
        response = self.submit_earn(daily["id"])
        self.assertEqual(response.status_code, 400)
        self.assertIn("该项目每天只能提交 1 次", response.get_json()["error"])
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        self.assertEqual(self.submit_earn(daily["id"], date=yesterday).status_code, 201)

        weekly = self.client.post(
            "/api/items/earn", json={"name": "每周上限", "points": 4, "icon": "book.svg", "cap_period": "week"}
        ).get_json()
        for day in (self.monday(1), self.monday(0)):
            self.assertEqual(self.submit_earn(weekly["id"], date=day.isoformat()).status_code, 201)
        response = self.submit_earn(weekly["id"])
        self.assertEqual(response.status_code, 400)
        self.assertIn("该项目每周只能提交 1 次", response.get_json()["error"])

        response = self.client.put(
            f"/api/items/earn/{daily['id']}",
            json={"name": "每日上限", "points": 3, "icon": "book.svg", "cap_period": "hour"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("提交上限周期只能选", response.get_json()["error"])

        updated = self.client.put(
            f"/api/items/earn/{daily['id']}",
            json={"name": "每日上限", "points": 3, "icon": "book.svg", "count_days_once": 1},
        ).get_json()
        self.assertEqual((updated["cap_period"], updated["cap_count"], updated["count_days_once"]), ("", 1, 1))
        self.assertEqual(self.submit_earn(daily["id"], date=yesterday).status_code, 201)

        for item in (daily, weekly):
            self.client.delete(f"/api/items/earn/{item['id']}")

    def test_cap_count_allows_several_submissions_per_calendar_window(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        monthly = self.client.post(
            "/api/items/earn",
            json={"name": "每月上限", "points": 2, "icon": "book.svg", "cap_period": "month", "cap_count": 2},
        ).get_json()
        self.assertEqual((monthly["cap_period"], monthly["cap_count"]), ("month", 2))

        today = date.today()
        month_start = today.replace(day=1)
        second_day = min(month_start + timedelta(days=1), today)
        self.assertEqual(self.submit_earn(monthly["id"], date=month_start.isoformat()).status_code, 201)
        self.assertEqual(self.submit_earn(monthly["id"], date=second_day.isoformat()).status_code, 201)
        response = self.submit_earn(monthly["id"])
        self.assertEqual(response.status_code, 400)
        self.assertIn("该项目每月只能提交 2 次", response.get_json()["error"])
        last_month = (month_start - timedelta(days=1)).isoformat()
        self.assertEqual(self.submit_earn(monthly["id"], date=last_month).status_code, 201)

        quarterly = self.client.post(
            "/api/items/earn", json={"name": "每季上限", "points": 6, "icon": "book.svg", "cap_period": "quarter"}
        ).get_json()
        self.assertEqual(quarterly["cap_count"], 1)
        quarter_start = today.replace(month=(today.month - 1) // 3 * 3 + 1, day=1)
        self.assertEqual(self.submit_earn(quarterly["id"], date=quarter_start.isoformat()).status_code, 201)
        response = self.submit_earn(quarterly["id"])
        self.assertEqual(response.status_code, 400)
        self.assertIn("该项目每季度只能提交 1 次", response.get_json()["error"])
        self.assertEqual(
            self.submit_earn(quarterly["id"], date=(quarter_start - timedelta(days=1)).isoformat()).status_code, 201
        )

        cap_window = self.app_module.cap_window
        self.assertEqual(cap_window("day", "2026-10-13"), ("2026-10-13", "2026-10-13"))
        self.assertEqual(cap_window("week", "2026-10-13"), ("2026-10-12", "2026-10-18"))
        self.assertEqual(cap_window("month", "2026-10-13"), ("2026-10-01", "2026-10-31"))
        self.assertEqual(cap_window("month", "2026-01-31"), ("2026-01-01", "2026-01-31"))
        self.assertEqual(cap_window("quarter", "2026-11-05"), ("2026-10-01", "2026-12-31"))
        self.assertEqual(cap_window("half_year", "2026-01-02"), ("2026-01-01", "2026-06-30"))
        self.assertEqual(cap_window("year", "2026-12-31"), ("2026-01-01", "2026-12-31"))

        for item in (monthly, quarterly):
            self.client.delete(f"/api/items/earn/{item['id']}")

    def test_cap_period_applies_to_requests_and_approval(self):
        child = self.create_child()
        item = self.client.post(
            "/api/items/earn", json={"name": "打卡项目", "points": 5, "icon": "book.svg", "cap_period": "day"}
        ).get_json()
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "child1", "password": "child123"})
        response = self.submit_earn(item["id"])
        self.assertEqual(response.status_code, 202)
        request_id = response.get_json()["request_id"]
        response = self.submit_earn(item["id"])
        self.assertEqual(response.status_code, 400)
        self.assertIn("该项目每天只能提交 1 次", response.get_json()["error"])

        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        self.assertEqual(self.client.post(f"/api/requests/{request_id}/approve").status_code, 200)

        open_item = self.create_item("earn", "后设上限", 7)
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "child1", "password": "child123"})
        late_request = self.submit_earn(open_item).get_json()["request_id"]
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        self.assertEqual(self.submit_earn(open_item).status_code, 201)
        self.client.put(
            f"/api/items/earn/{open_item}",
            json={"name": "后设上限", "points": 7, "icon": "book.svg", "cap_period": "day"},
        )
        response = self.client.post(f"/api/requests/{late_request}/approve")
        self.assertEqual(response.status_code, 400)
        self.assertIn("该项目每天只能提交 1 次", response.get_json()["error"])
        self.client.delete(f"/api/items/earn/{open_item}")
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_backdated_log_respects_the_submit_cap(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.client.post(
            "/api/items/earn", json={"name": "每日上限", "points": 3, "icon": "book.svg", "cap_period": "day"}
        ).get_json()
        today = date.today().isoformat()
        self.assertEqual(
            self.client.post("/api/transactions", json={"kind": "earn", "item_id": item["id"], "date": today}).status_code,
            201,
        )
        response = self.client.post(
            "/api/transactions",
            json={"kind": "manual", "item_kind": "earn", "item_id": item["id"], "date": today},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("该项目每天只能提交 1 次", response.get_json()["error"])

    def test_regrading_a_month_keeps_the_week_it_already_settled(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.client.post(
            "/api/items/deduct",
            json={
                "name": "混合规则",
                "points": 5,
                "icon": "book.svg",
                "target_unit": "week",
                "week_target": 1,
                "week_bonus": 2,
                "streak_weeks": 1,
                "streak_bonus": 3,
                "streak_unit": "month",
            },
        ).get_json()
        # 规则起算于一个多月前那个完整月份的 1 号，于是本月和整整一周都已闭合，
        # 同时都还在月档 12 期的回看范围之内。
        month_start = self.app_module.period_shift(
            "month", 1, self.app_module.period_start("month", self.monday(4).isoformat()), -1
        )
        week_start = self.app_module.full_window_start("week", 1, month_start)
        self.backdate_rule(item["id"], date.fromisoformat(month_start), "deduct")
        log_day = date.fromisoformat(week_start) + timedelta(days=1)
        self.deduct_on(item["id"], log_day, times=3)
        self.settle("week", week_start)
        self.settle("month", month_start)
        settled = self.streak_records("over_cap_penalty")
        self.assertEqual([row["amount"] for row in settled], [-4])
        self.assertEqual([row["amount"] for row in self.streak_records("deduct_month_streak")], [-3])

        # 单个档位重新核算，不能把另一个档位已发的窗口一起吃掉。
        records = self.settle("month", log_day.replace(day=1).isoformat(), force=True)["records"]
        self.assertEqual([row["amount"] for row in records if row["source"] == "over_cap_penalty"], [-4])
        self.assertEqual([row["amount"] for row in records if row["source"] == "deduct_month_streak"], [-3])

        records = self.settle("week", self.monday(4).isoformat(), force=True)["records"]
        self.assertEqual([row["amount"] for row in records if row["source"] == "deduct_month_streak"], [-3])

    def test_a_second_settle_walk_cannot_pay_one_window_twice(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.client.post(
            "/api/items/earn",
            json={"name": "阅读", "points": 2, "icon": "book.svg", "week_target": 2, "week_bonus": 4},
        ).get_json()
        self.backdate_rule(item["id"], self.monday(1))
        self.grant(item["id"], weeks_back=1, times=3)
        paid = [row for row in self.settle()["records"] if row["source"] == "weekly_bonus"]
        self.assertEqual([(row["title"], row["amount"]) for row in paid], [("阅读 3/2", 4)])

        # 以前水位行丢了就足以把同一期再发一遍，现在由记录上的唯一索引拦住。
        with type(self).connection() as conn:
            conn.execute("DELETE FROM weekly_settlements WHERE item_kind = 'earn' AND item_id = ?", (item["id"],))
        self.settle()
        self.assertEqual(len([row for row in self.state()["records"] if row["source"] == "weekly_bonus"]), 1)
        self.assertEqual(self.state()["total_points"], 3 * 2 + 4)
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_cap_and_day_count_edits_keep_weekly_rule_anchor(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.client.post(
            "/api/items/earn",
            json={"name": "锚点项目", "points": 5, "icon": "book.svg", "week_target": 2, "week_bonus": 3},
        ).get_json()
        self.backdate_rule(item["id"], self.monday(2))
        anchor = f"{self.monday(2).isoformat()}T00:00:00"
        self.assertEqual(self.earn_item_row(item["id"])["rule_set_at"], anchor)

        capped = self.client.put(
            f"/api/items/earn/{item['id']}",
            json={
                "name": "锚点项目改名",
                "points": 5,
                "icon": "book.svg",
                "week_target": 2,
                "week_bonus": 3,
                "cap_period": "day",
                "cap_count": 2,
                "count_days_once": 1,
            },
        ).get_json()
        self.assertEqual((capped["cap_period"], capped["cap_count"], capped["count_days_once"]), ("day", 2, 1))
        self.assertEqual(capped["rule_set_at"], anchor)

        reanchored = self.client.put(
            f"/api/items/earn/{item['id']}",
            json={
                "name": "锚点项目改名",
                "points": 5,
                "icon": "book.svg",
                "week_target": 3,
                "week_bonus": 3,
                "cap_period": "day",
                "cap_count": 2,
                "count_days_once": 1,
            },
        ).get_json()
        self.assertEqual(reanchored["week_target"], 3)
        self.assertNotEqual(reanchored["rule_set_at"], anchor)
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_items_can_be_reordered(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        created = [
            self.client.post("/api/items/earn", json={"name": f"排序{index}", "points": 2, "icon": "book.svg"}).get_json()
            for index in (1, 2, 3)
        ]
        library = [row["id"] for row in self.state()["item_library"]["earn"]]
        self.assertEqual(library[-3:], [item["id"] for item in created])

        head = [created[1]["id"], created[2]["id"], created[0]["id"]]
        expected = head + [item_id for item_id in library if item_id not in head]
        response = self.client.post("/api/items/earn/reorder", json={"ids": expected})
        self.assertEqual([row["id"] for row in response.get_json()["item_library"]["earn"]], expected)
        self.assertEqual([row["id"] for row in self.state()["earn_items"]][:3], head)

        moved = [
            row["id"] for row in self.client.post("/api/items/earn/reorder", json={"ids": [created[0]["id"]]}).get_json()["item_library"]["earn"]
        ]
        self.assertEqual(moved[0], created[0]["id"])
        self.assertEqual(sorted(moved), sorted(library))

        self.assertEqual(self.client.post("/api/items/task/reorder", json={"ids": []}).status_code, 404)
        self.assertEqual(self.client.post("/api/items/earn/reorder", json={}).status_code, 400)
        self.assertEqual(self.client.post("/api/items/earn/reorder", json={"ids": ["x"]}).status_code, 400)
        for item in created:
            self.client.delete(f"/api/items/earn/{item['id']}")

    def test_analytics_series_and_distribution(self):
        first = self.create_child(username="kid-c")
        second = self.create_child(username="kid-d")
        earn_id = self.create_item("earn", "完成作业", 10)
        self.client.post("/api/auth/select-child", json={"child_id": first["id"]})
        self.client.post("/api/transactions", json={"kind": "earn", "item_id": earn_id})
        self.client.post("/api/transactions", json={"kind": "deduct", "points": 3, "name": "临时扣分"})
        self.client.post("/api/auth/select-child", json={"child_id": second["id"]})
        self.client.post("/api/transactions", json={"kind": "earn", "item_id": earn_id})

        data = self.client.get("/api/analytics?days=7").get_json()
        self.assertEqual(data["days"], 7)
        self.assertEqual(len(data["series"]), 2)
        self.assertEqual(len(data["dates"]), 7)
        self.assertEqual(data["summary"]["gained"], 20)
        self.assertEqual({row["account_id"] for row in data["comparison"]}, {first["id"], second["id"]})
        project_names = [row["name"] for row in data["projects"]["earn"]]
        self.assertIn("完成作业", project_names)
        periods = data["item_periods"]
        self.assertIn("完成作业", [row["name"] for row in periods["items"]["earn"]])
        self.assertEqual(periods["other"], {"count": 1, "gained": 0, "lost": 3})
        self.assertEqual(len(periods["periods"]), self.app_module.ITEM_PERIOD_COUNT)

        today = date.today().isoformat()
        daily = self.client.get("/api/analytics?days=7&unit=day&periods=3").get_json()["item_periods"]
        self.assertEqual(daily["unit"], "day")
        self.assertEqual(daily["periods"][-1]["label"], today)
        rows = [item for item in daily["items"]["earn"] if item["name"] == "完成作业"]
        # 这张表一行一个项目、一列一个孩子：同一个项目不会拆成两行，也不会把两个孩子的积分并在一起。
        self.assertEqual(len(rows), 1)
        columns = [child["id"] for child in daily["children"]]
        cells = {columns[index]: cell for index, cell in enumerate(rows[0]["cells"])}
        self.assertEqual(
            [(cells[first["id"]]["measure"], cells[first["id"]]["count"], cells[first["id"]]["coins"]),
             (cells[second["id"]]["measure"], cells[second["id"]]["count"], cells[second["id"]]["coins"])],
            [("count", 1, 10), ("count", 1, 10)],
        )
        self.assertEqual(self.client.get("/api/analytics?days=7&unit=hour").status_code, 400)
        self.assertEqual(self.client.get("/api/analytics?days=7&periods=0").status_code, 400)

        scoped = self.client.get(f"/api/analytics?days=7&account_id={first['id']}").get_json()
        self.assertEqual(scoped["summary"]["gained"], 10)
        self.assertEqual({row["account_id"] for row in scoped["comparison"]}, {first["id"]})
        self.assertEqual(self.client.get("/api/analytics?days=7&account_id=invalid").status_code, 400)
        admin_id = [row["id"] for row in self.client.get("/api/accounts").get_json()["accounts"] if row["role"] == "admin"][0]
        self.assertEqual(self.client.get(f"/api/analytics?days=7&account_id={admin_id}").status_code, 404)

        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "kid-c", "password": "child123"})
        child_data = self.client.get("/api/analytics?days=30&account_id=%s" % second["id"]).get_json()
        self.assertEqual([row["account_id"] for row in child_data["series"]], [first["id"]])

    def test_target_sheet_exports_workbook(self):
        sheet_namespace = "{%s}" % self.app_module.SHEET_NS
        long_rule = "每次+5 · 每周3次 · 超额每次+1 · 未达标-2 · 连续2期达标额外+3"
        payload = {
            "unit": "week",
            "date": date.today().isoformat(),
            "range": "本周 10/19 ~ 10/25",
            "heads": [["项目", "规则", "一", "二"], ["", "", "10/19", "10/20"]],
            "sheets": [
                {"name": "小明[测*试:?]", "rows": [["阅读", long_rule], ["口算", "每周10次"]]},
                {"name": "小明[测*试:?]", "rows": [["阅读", "每周3次"]]},
            ],
        }
        response = self.client.post("/api/targets/export", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, self.app_module.XLSX_MIME)
        self.assertIn("filename*=UTF-8''", response.headers["Content-Disposition"])

        archive = zipfile.ZipFile(io.BytesIO(response.data))
        self.assertEqual(archive.namelist().count("xl/worksheets/sheet2.xml"), 1)
        self.assertNotIn("xl/worksheets/sheet3.xml", archive.namelist())
        for part in archive.namelist():
            ElementTree.fromstring(archive.read(part))

        tabs = [element.get("name") for element in ElementTree.fromstring(archive.read("xl/workbook.xml")).iter(sheet_namespace + "sheet")]
        self.assertEqual(len(tabs), 2)
        self.assertEqual(len(set(tabs)), 2)
        self.assertNotRegex("".join(tabs), r"[\\/*?:\[\]]")

        sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        self.assertEqual(len(list(sheet.iter(sheet_namespace + "row"))), 5)
        self.assertEqual([element.get("ref") for element in sheet.iter(sheet_namespace + "mergeCell")], ["A1:D1"])
        self.assertEqual(next(sheet.iter(sheet_namespace + "pageSetup")).get("orientation"), "landscape")
        self.assertIn("每周10次", [element.text for element in sheet.iter(sheet_namespace + "t")])
        # 规则列不再是 24 字的短摘要，长规则整句写进工作簿。
        self.assertIn(long_rule, [element.text for element in sheet.iter(sheet_namespace + "t")])
        boxes = list(sheet.iter(sheet_namespace + "row"))[3]
        self.assertEqual(len(boxes.findall(sheet_namespace + "c")), 4)

        wide = dict(payload, heads=[["项目", "规则"] + [str(step) for step in range(200)], ["", ""] + [str(step) for step in range(200)]])
        wide_sheet = ElementTree.fromstring(zipfile.ZipFile(io.BytesIO(self.client.post("/api/targets/export", json=wide).data)).read("xl/worksheets/sheet1.xml"))
        wide_head = list(wide_sheet.iter(sheet_namespace + "row"))[1]
        self.assertEqual(len(wide_head.findall(sheet_namespace + "c")), self.app_module.TARGET_SHEET_MAX_COLUMNS)

        for broken, message in (
            (dict(payload, unit="day"), "导出周期"),
            (dict(payload, date="yesterday"), "导出日期"),
            (dict(payload, heads=[["项目", "目标", "一"], ["", "", "10/19", "10/20"]]), "表头"),
            (dict(payload, sheets=[{"name": "小明", "rows": []}]), "没有可导出的项目"),
        ):
            response = self.client.post("/api/targets/export", json=broken)
            self.assertEqual(response.status_code, 400)
            self.assertIn(message, response.get_json()["error"])

        child = self.create_child(username="kid-export")
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "kid-export", "password": "child123"})
        self.assertEqual(self.client.post("/api/targets/export", json=payload).status_code, 403)

    def test_target_sheet_columns_fit_their_content(self):
        # 列宽行高原先写死，长项目名折行进一行显示不下，现在一律按内容算。
        sheet_namespace = "{%s}" % self.app_module.SHEET_NS
        long_name = "阅读课外书" * 8
        payload = {
            "unit": "week",
            "date": date.today().isoformat(),
            "range": "本周 10/19 ~ 10/25",
            "heads": [["项目", "规则", "一"], ["", "", "10/19"]],
            "sheets": [{"name": "小明", "rows": [[long_name, "每周数量10次"], ["口算", "每天1次"]]}],
        }
        archive = zipfile.ZipFile(io.BytesIO(self.client.post("/api/targets/export", json=payload).data))
        sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        widths = {element.get("min"): float(element.get("width")) for element in sheet.iter(sheet_namespace + "col")}
        heights = {element.get("r"): float(element.get("ht")) for element in sheet.iter(sheet_namespace + "row") if element.get("ht")}

        self.assertEqual(widths["1"], self.app_module.XLSX_NAME_BOUNDS[1])
        self.assertEqual(widths["2"], self.app_module.XLSX_RULE_BOUNDS[0])
        self.assertGreaterEqual(widths["3"], self.app_module.xlsx_display_width("10/19"))
        self.assertGreater(heights["4"], heights["5"])
        self.assertEqual(next(sheet.iter(sheet_namespace + "tabColor")).get("rgb"), "FF24A37B")

    def target_status(self, child_id, items, columns):
        response = self.client.post(
            "/api/targets/status", json={"columns": columns, "sheets": [{"child": child_id, "items": items}]}
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()["cells"]

    def test_target_status_uses_each_projects_measure(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        monday = self.monday(1)
        days = [(monday + timedelta(days=step)).isoformat() for step in range(7)]
        times_id = self.create_item("earn", "整理房间", 5)
        quantity_id = self.quantity_item("阅读", points=2, week_target=10, target_measure="quantity")["id"]
        daily_id = self.client.post(
            "/api/items/earn", json={"name": "早睡", "points": 2, "icon": "bedtime.svg", "count_days_once": 1}
        ).get_json()["id"]
        deduct_id = self.create_item("deduct", "迟到", 3)
        for item_id, day in ((times_id, days[1]), (times_id, days[1]), (quantity_id, days[2])):
            payload = {"kind": "earn", "item_id": item_id, "date": day}
            if item_id == quantity_id:
                payload["quantity"] = 7
            self.assertEqual(self.client.post("/api/transactions", json=payload).status_code, 201)
        for day in (days[0], days[0], days[5]):
            self.assertEqual(
                self.client.post("/api/transactions", json={"kind": "earn", "item_id": daily_id, "date": day}).status_code,
                201,
            )
        self.deduct_on(deduct_id, monday + timedelta(days=3))

        keys = [f"earn:{times_id}", f"earn:{quantity_id}", f"earn:{daily_id}", f"deduct:{deduct_id}"]
        columns = [{"start": day, "end": day} for day in days]
        cells = self.target_status(child["id"], keys, columns)[str(child["id"])]
        self.assertEqual(cells[keys[0]], [0, 2, 0, 0, 0, 0, 0])
        # 数量口径按录入量合计，按天统计口径只数有记录的天数，扣分项目同样按次数。
        self.assertEqual(cells[keys[1]], [0, 0, 7, 0, 0, 0, 0])
        self.assertEqual(cells[keys[2]], [1, 0, 0, 0, 0, 1, 0])
        self.assertEqual(cells[keys[3]], [0, 0, 0, 1, 0, 0, 0])

        # 季度那种 7 天一格由页面切好区间送过来，两端都算进同一格。
        merged = self.target_status(child["id"], keys, [{"start": days[0], "end": days[6]}])[str(child["id"])]
        self.assertEqual([merged[key] for key in keys], [[2], [7], [2], [1]])
        # 结算记录不是完成量，格子只认真正的加/扣分。
        self.settle()
        self.assertEqual(self.target_status(child["id"], keys, [{"start": days[0], "end": days[6]}]), {str(child["id"]): merged})

    def test_target_status_checks_the_cells_it_is_given(self):
        child = self.create_child()
        item_id = self.create_item("earn", "整理房间", 5)
        today = date.today().isoformat()
        columns = [{"start": today, "end": today}]
        items = [f"earn:{item_id}"]
        for payload, message in (
            ({"columns": [], "sheets": [{"child": child["id"], "items": items}]}, "没有要统计的周期格子"),
            ({"columns": [{"start": "not-a-day", "end": today}], "sheets": [{"child": child["id"], "items": items}]}, "周期格子日期无效"),
            ({"columns": [{"start": today, "end": (date.today() - timedelta(days=1)).isoformat()}], "sheets": [{"child": child["id"], "items": items}]}, "起止日期反了"),
            ({"columns": columns, "sheets": [{"child": "abc", "items": items}]}, "孩子标识无效"),
            ({"columns": columns, "sheets": [{"child": child["id"], "items": ["earn:abc"]}]}, "统计的项目无效"),
            ({"columns": columns, "sheets": [{"child": child["id"], "items": ["reward:1"]}]}, "项目类型无效"),
            ({"columns": columns, "sheets": []}, "没有可统计的项目"),
        ):
            response = self.client.post("/api/targets/status", json=payload)
            self.assertEqual(response.status_code, 400, payload)
            self.assertIn(message, response.get_json()["error"])

        # 删掉的项目不再回格子，整个孩子因此没有可统计的行。
        missing = self.target_status(child["id"], ["earn:%s" % (item_id + 999)], columns)
        self.assertEqual(missing, {})

        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": child["username"], "password": "child123"})
        self.assertEqual(self.client.post("/api/targets/status", json={"columns": columns, "sheets": [{"child": child["id"], "items": items}]}).status_code, 403)

    def test_target_sheet_export_carries_the_status_marks(self):
        sheet_namespace = "{%s}" % self.app_module.SHEET_NS
        payload = {
            "unit": "week",
            "date": date.today().isoformat(),
            "range": "本周 10/19 ~ 10/25",
            "heads": [["项目", "规则", "一", "二"], ["", "", "10/19", "10/20"]],
            "sheets": [
                {
                    "name": "小明",
                    "rows": [["阅读", "每周10页", "", "12"], ["早睡", "每天", "✓", ""], ["迟到", "每周≤2次", "✗", ""]],
                }
            ],
        }
        sheet = ElementTree.fromstring(
            zipfile.ZipFile(io.BytesIO(self.client.post("/api/targets/export", json=payload).data)).read("xl/worksheets/sheet1.xml")
        )
        texts = [element.text for element in sheet.iter(sheet_namespace + "t")]
        # 三档标记都是页面自己算好的字符串，工作簿原样带上，不再判一遍口径。
        self.assertIn("12", texts)
        self.assertEqual([text for text in texts if text in ("✓", "✗")], ["✓", "✗"])
        # 空档只留边框：勾掉「显示完成情况」时整张表退成纯手写。
        self.assertEqual(
            [
                cell.get("r")
                for cell in sheet.iter(sheet_namespace + "c")
                if cell.get("s") == "4" and cell.find(sheet_namespace + "is") is None
            ],
            ["C4", "D5", "D6"],
        )

    def test_target_sheet_export_survives_untypeable_characters(self):
        # 项目名和昵称来自浏览器，而 XML 1.0 不允许携带粘贴一起带进来的控制字符。
        payload = {
            "unit": "week",
            "date": date.today().isoformat(),
            "range": "本周 \x0b10/19 ~ 10/25",
            "heads": [["项目\x02", "规则", "一"], ["", "", "10/19"]],
            "sheets": [{"name": "小明\x07", "rows": [["阅读\x00", "每周3次"]]}],
        }
        response = self.client.post("/api/targets/export", json=payload)
        self.assertEqual(response.status_code, 200)
        archive = zipfile.ZipFile(io.BytesIO(response.data))
        for part in archive.namelist():
            ElementTree.fromstring(archive.read(part))

    def test_duplicate_tab_names_stay_inside_excel_limits(self):
        used: set[str] = set()
        names = [self.app_module.xlsx_tab_name("重复的孩子名字" * 6, index, used) for index in range(1, 13)]
        self.assertTrue(all(len(name) <= 31 for name in names), names)
        self.assertEqual(len({name.lower() for name in names}), len(names))

    def test_number_fields_reject_values_sqlite_cannot_store(self):
        self.create_child(username="kid-bounds")
        for payload, hint in (
            ({"kind": "earn", "name": "整理房间", "item_id": "abc"}, "项目"),
            ({"kind": "earn", "name": "整理房间", "points": 10 ** 30}, "积分"),
            # JSON 的 1e999 会解析成 inf，int(inf) 抛的是 OverflowError。
            ({"kind": "earn", "name": "整理房间", "points": float("inf")}, "积分"),
            ({"kind": "exchange", "reward_id": 10 ** 30}, "项目"),
        ):
            response = self.client.post("/api/transactions", json=payload)
            self.assertEqual(response.status_code, 400, payload)
            error = response.get_json()["error"]
            self.assertIn(hint, error)
            # 解释器自己的报错原文绝不能出现在孩子的屏幕上。
            self.assertNotIn("int()", error)

    def test_oversized_request_still_answers_in_json(self):
        response = self.client.post("/api/transactions", json={"kind": "earn", "name": "x" * 2_000_000})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.get_json()["error"], "提交内容过大，请减少后重试")

    def test_session_key_is_created_once_and_reused(self):
        # 写死在源码里的密钥谁都能签出管理员 cookie，所以没配置时要随机生成并落盘复用。
        module = self.app_module
        first = module.generated_secret_key()
        self.assertEqual(len(first), 64)
        self.assertEqual(first, module.generated_secret_key())
        self.assertEqual((module.DATA_DIR / "secret_key").read_text(encoding="utf-8").strip(), first)

    def test_recent_flow_lists_gains_and_losses(self):
        child = self.create_child()
        self.client.post("/api/transactions", json={"kind": "earn", "points": 12, "name": "整理房间"})
        self.client.post("/api/transactions", json={"kind": "deduct", "points": 4, "name": "迟到"})
        flow = self.state()["recent_flow"]
        self.assertEqual(flow["gained_total"], 12)
        self.assertEqual(flow["lost_total"], 4)
        self.assertEqual(flow["net"], 8)
        self.assertEqual([row["name"] for row in flow["gained_items"]], ["整理房间"])
        self.assertEqual([row["name"] for row in flow["lost_items"]], ["迟到"])
        self.assertEqual(flow["gained_items"][0]["children"][str(child["id"])], {"count": 1, "total": 12})
        self.assertEqual(flow["lost_items"][0]["children"][str(child["id"])], {"count": 1, "total": -4})

    def test_recent_flow_columns_follow_the_viewer(self):
        first = self.create_child(username="kid-flow-a", display_name="甲")
        second = self.create_child(username="kid-flow-b", display_name="乙")
        item = self.create_item("earn", "共同项目", 2)
        for child in (first, second):
            self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
            self.client.post("/api/transactions", json={"kind": "earn", "item_id": item})
        flow = self.state()["recent_flow"]
        self.assertEqual([row["id"] for row in flow["children"]], [first["id"], second["id"]])
        row = next(item for item in flow["gained_items"] if item["name"] == "共同项目")
        self.assertEqual(row["children"][str(first["id"])]["total"], 2)
        self.assertEqual(row["children"][str(second["id"])]["total"], 2)

        # 孩子账号只看自己那一列，不能从首页看到兄弟姐妹的明细。
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "kid-flow-a", "password": "child123"})
        own = self.state()["recent_flow"]
        self.assertEqual([row["id"] for row in own["children"]], [first["id"]])
        own_row = next(item for item in own["gained_items"] if item["name"] == "共同项目")
        self.assertEqual(list(own_row["children"]), [str(first["id"])])

    def test_child_state_hides_sibling_accounts(self):
        self.create_child(username="kid-state-a", display_name="甲")
        self.create_child(username="kid-state-b", display_name="乙")
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "kid-state-a", "password": "child123"})

        # 孩子账号不需要知道兄弟姐妹的账号资料，账号列表只留给管理员。
        self.assertEqual(self.state()["children"], [])

    def test_custom_image_routes_find_uploaded_assets(self):
        for asset in ("child-boy", "child-girl", "adult-male", "adult-female"):
            response = self.client.get(f"/custom-assets/{asset}")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.content_type.startswith("image/"))
            response.close()

    def test_shipped_images_cache_only_when_they_exist(self):
        hit = self.client.get("/static/illustrations/points.svg")
        self.assertEqual(hit.status_code, 200)
        self.assertEqual(hit.headers["Cache-Control"], "public, max-age=86400")
        hit.close()
        miss = self.client.get("/static/illustrations/missing-icon.svg")
        self.assertEqual(miss.status_code, 404)
        self.assertNotEqual(miss.headers.get("Cache-Control"), "public, max-age=86400")


class ItemLibraryMigrationTests(unittest.TestCase):
    def build_legacy_database(self, path: Path) -> None:
        with contextlib.closing(sqlite3.connect(path)) as conn:
            conn.executescript(
                """
                CREATE TABLE accounts (id INTEGER PRIMARY KEY, username TEXT UNIQUE, password_hash TEXT, display_name TEXT, role TEXT, active INTEGER DEFAULT 1, created_at TEXT, avatar TEXT DEFAULT 'boy');
                CREATE TABLE earn_items (id INTEGER PRIMARY KEY, account_id INTEGER, name TEXT, points INTEGER, icon TEXT DEFAULT 'points.svg');
                CREATE TABLE deduct_items (id INTEGER PRIMARY KEY, account_id INTEGER, name TEXT, points INTEGER, icon TEXT DEFAULT 'warning.svg');
                CREATE TABLE rewards (id INTEGER PRIMARY KEY, account_id INTEGER, name TEXT, points INTEGER, icon TEXT DEFAULT 'gift.svg');
                CREATE TABLE records (id INTEGER PRIMARY KEY, account_id INTEGER, title TEXT, amount INTEGER, type TEXT, date TEXT, time TEXT, created_at TEXT);
                CREATE TABLE point_requests (id INTEGER PRIMARY KEY, account_id INTEGER, requester_id INTEGER, kind TEXT, title TEXT, amount INTEGER, type TEXT, date TEXT, time TEXT, status TEXT, created_at TEXT, reviewed_at TEXT, reviewer_id INTEGER, reject_reason TEXT);
                CREATE TABLE account_logs (id INTEGER PRIMARY KEY, actor_id INTEGER, actor_username TEXT, actor_name TEXT, target_id INTEGER, target_username TEXT, target_name TEXT, target_role TEXT, action TEXT, created_at TEXT);
                CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
                """
            )
            conn.execute("INSERT INTO accounts(id, username, password_hash, display_name, role, created_at) VALUES (1, 'admin', 'x', '管理员', 'admin', '2026-01-01T00:00:00')")
            conn.execute("INSERT INTO accounts(id, username, password_hash, display_name, role, created_at) VALUES (2, 'kida', 'x', '小红', 'child', '2026-01-01T00:00:00')")
            conn.execute("INSERT INTO accounts(id, username, password_hash, display_name, role, created_at) VALUES (3, 'kidb', 'x', '小蓝', 'child', '2026-01-01T00:00:00')")
            conn.executemany(
                "INSERT INTO earn_items(id, account_id, name, points, icon) VALUES (?, ?, ?, ?, ?)",
                [(1, 2, "完成作业", 10, "homework.svg"), (2, 2, "阅读30分钟", 5, "book.svg"), (3, 3, "完成作业", 10, "homework.svg"), (4, 3, "练琴", 8, "music")],
            )
            conn.executemany(
                "INSERT INTO deduct_items(id, account_id, name, points, icon) VALUES (?, ?, ?, ?, ?)",
                [(5, 2, "迟到", 5, "warning.svg"), (6, 3, "迟到", 5, "warning.svg")],
            )
            conn.executemany(
                "INSERT INTO rewards(id, account_id, name, points, icon) VALUES (?, ?, ?, ?, ?)",
                [(7, 2, "看电视30分钟", 50, "television.svg"), (8, 3, "玩游戏1小时", 100, "game.svg")],
            )
            conn.execute(
                "INSERT INTO records(account_id, title, amount, type, date, time, created_at) VALUES (2, '完成作业', 10, 'income', '2026-01-05', '10:00', '2026-01-05T10:00:00')"
            )
            conn.execute(
                "INSERT INTO records(account_id, title, amount, type, date, time, created_at) VALUES (3, '看电视30分钟', -50, 'expense', '2026-01-06', '10:00', '2026-01-06T10:00:00')"
            )
            conn.commit()

    def test_legacy_items_merge_into_library_with_bindings(self):
        import app as app_module

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            database = data_dir / "points.db"
            self.build_legacy_database(database)
            original = (app_module.DATA_DIR, app_module.DATABASE_PATH)
            app_module.DATA_DIR, app_module.DATABASE_PATH = data_dir, database
            try:
                app_module.init_db()
                with contextlib.closing(sqlite3.connect(database)) as conn:
                    conn.row_factory = sqlite3.Row
                    columns = {row["name"] for row in conn.execute("PRAGMA table_info(earn_items)").fetchall()}
                    self.assertNotIn("account_id", columns)
                    library = {row["name"]: row["id"] for row in conn.execute("SELECT id, name FROM earn_items").fetchall()}
                    self.assertEqual(library.keys(), {"完成作业", "阅读30分钟", "练琴"})
                    exclusions = {
                        (row["account_id"], row["item_kind"], row["item_id"])
                        for row in conn.execute("SELECT account_id, item_kind, item_id FROM item_bindings WHERE enabled = 0").fetchall()
                    }
                    self.assertIn((2, "earn", library["练琴"]), exclusions)
                    self.assertIn((3, "earn", library["阅读30分钟"]), exclusions)
                    record = conn.execute("SELECT item_kind, item_id, source FROM records WHERE account_id = 2").fetchone()
                    self.assertEqual((record["item_kind"], record["item_id"], record["source"]), ("earn", library["完成作业"], "earn"))
                    icons = {row["icon"] for row in conn.execute("SELECT icon FROM earn_items").fetchall()}
                    self.assertTrue(all(icon.endswith(".svg") for icon in icons))
            finally:
                app_module.DATA_DIR, app_module.DATABASE_PATH = original

    def test_upgrade_keeps_one_payout_per_settled_window(self):
        import app as app_module

        rows = [
            ("00:01", "target_streak"), ("00:02", "target_streak"), ("00:03", "target_streak"),
            ("00:04", "weekly_bonus"), ("00:05", "weekly_bonus"),
            # 普通记录本来就允许同一项目同一天好几笔，所以不进这个唯一索引。
            ("00:06", "earn"), ("00:07", "earn"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "points.db"
            with contextlib.closing(sqlite3.connect(database)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute(
                    "CREATE TABLE records ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER, item_kind TEXT, "
                    "item_id INTEGER, date TEXT, time TEXT, source TEXT)"
                )
                conn.executemany(
                    "INSERT INTO records(account_id, item_kind, item_id, date, time, source) "
                    "VALUES (2, 'earn', 7, '2026-08-30', ?, ?)",
                    rows,
                )
                app_module.ensure_settlement_uniqueness(conn)
                kept = [(row["time"], row["source"]) for row in conn.execute("SELECT time, source FROM records ORDER BY id")]
                self.assertEqual(kept, [("00:01", "target_streak"), ("00:04", "weekly_bonus"), ("00:06", "earn"), ("00:07", "earn")])
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "INSERT INTO records(account_id, item_kind, item_id, date, time, source) "
                        "VALUES (2, 'earn', 7, '2026-08-30', '00:08', 'target_streak')"
                    )
                conn.execute(
                    "INSERT INTO records(account_id, item_kind, item_id, date, time, source) "
                    "VALUES (2, 'earn', 7, '2026-08-30', '00:09', 'earn')"
                )

    def test_upgrade_rebuilds_the_payout_index_when_a_source_joins(self):
        import app as app_module

        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "points.db"
            with contextlib.closing(sqlite3.connect(database)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute(
                    "CREATE TABLE records ("
                    "id INTEGER PRIMARY KEY, account_id INTEGER, item_kind TEXT, "
                    "item_id INTEGER, date TEXT, source TEXT)"
                )
                # 老版本建的唯一索引不含罚分那个 source，光靠 IF NOT EXISTS 会让它继续漏拦。
                conn.execute(
                    "CREATE UNIQUE INDEX records_settlement_once ON records(account_id, item_kind, item_id, date, source)"
                    " WHERE source IN ('weekly_bonus', 'target_streak')"
                )
                app_module.ensure_settlement_uniqueness(conn)
                stored = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'records_settlement_once'"
                ).fetchone()["sql"]
                self.assertIn(app_module.TARGET_STREAK_PENALTY_SOURCE, stored)
                conn.execute(
                    "INSERT INTO records(account_id, item_kind, item_id, date, source) "
                    "VALUES (2, 'earn', 7, '2026-08-30', 'target_streak_penalty')"
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "INSERT INTO records(account_id, item_kind, item_id, date, source) "
                        "VALUES (2, 'earn', 7, '2026-08-30', 'target_streak_penalty')"
                    )


if __name__ == "__main__":
    unittest.main()
