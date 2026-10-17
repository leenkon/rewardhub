import base64
import contextlib
import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

BOOTSTRAP_DIR = None


def setUpModule():
    """Point the module-level database at a throwaway directory before app is imported."""
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
            migrate_legacy_admin_account,
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
        cls.migrate_legacy_admin_account = migrate_legacy_admin_account
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

    def test_install_credentials_replace_only_untouched_legacy_admin(self):
        os.environ["ADMIN_USERNAME"] = "new-admin"
        os.environ["ADMIN_PASSWORD"] = "new-secret"
        try:
            with type(self).connection() as conn:
                type(self).migrate_legacy_admin_account(conn)
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
        self.assertEqual(state["version"], "0.8.2")
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
        self.state()
        self.assertTrue(self.settlement_rows(item["id"]))

        # Grading is derived from the logs, so wiping a child's history must wipe its water marks too.
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

        record_id = self.state()["records"][0]["id"]
        response = self.client.post(f"/api/transactions/{record_id}/undo")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["total_points"], 10)

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

    def test_builtin_delivery_icon_is_allowed_and_unknown_icon_is_rejected(self):
        self.create_child(username="icon-child", display_name="Icon Child", password="child123")
        response = self.client.post(
            "/api/items/earn",
            json={"name": "快递取件", "points": 8, "icon": "emoji:delivery"},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["icon"], "emoji:delivery")
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

    def backdate_rule(self, item_id, day, kind="earn"):
        with type(self).connection() as conn:
            conn.execute(
                f"UPDATE {kind}_items SET rule_set_at = ? WHERE id = ?",
                (f"{day.isoformat()}T00:00:00", item_id),
            )

    def settlement_rows(self, item_id, kind="earn"):
        with type(self).connection() as conn:
            return conn.execute(
                "SELECT week_start, bonus, penalty FROM weekly_settlements WHERE item_kind = ? AND item_id = ? ORDER BY week_start",
                (kind, item_id),
            ).fetchall()

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
        self.assertEqual(row["today_count"], 2)
        self.assertEqual(row["week_count"], 2)

        self.grant(item["id"], weeks_back=1)
        row = self.earn_item_row(item["id"])
        self.assertEqual(row["today_count"], 2)
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

        data = self.state()
        bonuses = [row for row in data["records"] if row["source"] == "weekly_bonus"]
        self.assertEqual([row["amount"] for row in bonuses], [4])
        self.assertEqual(data["total_points"], 3 * 5 + 4)
        self.assertEqual(len(self.settlement_rows(item["id"])), 1)
        self.assertEqual(len(self.state()["records"]), len(data["records"]))
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

        data = self.state()
        penalties = [row for row in data["records"] if row["source"] == "weekly_penalty"]
        self.assertEqual([row["title"] for row in penalties], ["达标项目 未达标 1/5"])
        self.assertEqual(data["total_points"], 2 * 5 - 3)
        self.assertEqual([row["week_start"] for row in self.settlement_rows(item["id"])], [self.monday(1).isoformat()])
        self.assertEqual(len(self.state()["records"]), len(data["records"]))
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
        rows = self.settlement_rows(item["id"])
        self.assertEqual([row["week_start"] for row in rows], [earliest, previous])
        self.assertEqual([row["penalty"] for row in rows], [3, 3])
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_settlement_skips_items_disabled_for_the_child(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.client.post(
            "/api/items/earn",
            json={"name": "停用项目", "points": 5, "icon": "book.svg", "week_target": 5, "week_penalty": 3},
        ).get_json()
        self.grant(item["id"], weeks_back=1)
        self.backdate_rule(item["id"], self.monday(1))
        self.client.put(
            f"/api/item-bindings/{child['id']}",
            json={"bindings": [{"kind": "earn", "item_id": item["id"], "enabled": False}]},
        )

        # A project turned off for this child earns nothing and costs nothing, even with logs behind it.
        self.state()
        self.assertEqual(self.settlement_rows(item["id"]), [])
        self.assertEqual([row for row in self.state()["records"] if row["source"] == "weekly_penalty"], [])

        self.client.put(
            f"/api/item-bindings/{child['id']}",
            json={"bindings": [{"kind": "earn", "item_id": item["id"], "enabled": True}]},
        )
        self.assertEqual(
            [row["amount"] for row in self.state()["records"] if row["source"] == "weekly_penalty"],
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
        # The rule reaches six weeks back, but the child's only log sits in last week.
        self.backdate_rule(item["id"], self.monday(6))
        self.assertEqual(
            [row["date"] for row in self.state()["records"] if row["source"] == "weekly_penalty"],
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
            [row["amount"] for row in self.state()["records"] if row["source"] == "weekly_bonus"],
            [5],
        )

        # The approved log joins that closed window, so its reward is recomputed rather than left behind.
        self.assertEqual(self.client.post(f"/api/requests/{request_id}/approve", json={}).status_code, 200)
        self.assertEqual(
            [row["amount"] for row in self.state()["records"] if row["source"] == "weekly_bonus"],
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
        settled = self.state()
        self.assertEqual([row["amount"] for row in settled["records"] if row["source"] == "weekly_bonus"], [5])

        # Re-anchoring the rule to today means older windows are no longer graded, so a back-dated log there
        # must leave the already-paid reward alone instead of clearing a window it cannot rebuild.
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
        self.state()
        self.assertEqual([(row["week_start"], row["bonus"]) for row in self.settlement_rows(item["id"])], [(previous, 5)])
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_target_rule_quarter_window_uses_calendar_quarters(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        app = self.app_module
        current = app.period_start("quarter", date.today().isoformat())
        previous = app.period_shift("quarter", 1, current, -1)
        # Quarters reuse the month anchor, so every window starts on January, April, July or October.
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
        self.state()
        self.assertEqual(
            [(row["title"], row["amount"]) for row in self.state()["records"] if row["source"] == "weekly_bonus"],
            [("季度项目 超额 1 次", 5)],
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
        bonuses = [row for row in self.state()["records"] if row["source"] == "weekly_bonus"]
        self.assertEqual([(row["title"], row["amount"]) for row in bonuses], [("间隔项目 超额 3 次", 5)])
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_target_rule_validates_unit_and_measure(self):
        rejected = self.client.post(
            "/api/items/earn",
            json={"name": "年度项目", "points": 5, "icon": "book.svg", "week_target": 2, "target_unit": "year"},
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("目标周期只能选天、周、月或季", rejected.get_json()["error"])
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
        item = self.client.post(
            "/api/items/earn",
            json={"name": "默认周期", "points": 5, "icon": "book.svg", "week_target": 2, "target_every": 0, "bonus_every": 0},
        ).get_json()
        self.assertEqual([item["target_unit"], item["target_every"], item["bonus_every"]], ["week", 1, 1])
        self.client.delete(f"/api/items/earn/{item['id']}")

    def streak_records(self, source):
        return [row for row in self.state()["records"] if row["source"] == source]

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

        streaks = self.streak_records("target_streak")
        self.assertEqual([row["title"] for row in streaks], ["连续项目 连续达标 2 期"])
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

        # Four straight days pay once at the third day; the fourth day starts the next run.
        self.assertEqual(
            [(row["title"], row["amount"], row["date"]) for row in self.streak_records("target_streak")],
            [("每日连续 连续达标 3 期", 6, (date.today() - timedelta(days=2)).isoformat())],
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
                "streak_unit": "year",
            },
        )
        self.assertEqual(bad_unit.status_code, 400)
        self.assertIn("连续周期只能选天、周、月或季", bad_unit.get_json()["error"])

        self.backdate_rule(item["id"], self.monday(2), kind="deduct")
        for weeks_back in (2, 1):
            day = self.monday(weeks_back) + timedelta(days=1)
            response = self.client.post(
                "/api/transactions", json={"kind": "deduct", "item_id": item["id"], "date": day.isoformat()}
            )
            self.assertEqual(response.status_code, 201)

        payouts = self.streak_records("deduct_week_streak")
        self.assertEqual([(row["title"], row["amount"], row["type"]) for row in payouts], [("连续迟到 连续扣分 2 周", -5, "expense")])
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
        payouts = self.streak_records("deduct_month_streak")
        self.assertEqual([(row["title"], row["amount"], row["type"]) for row in payouts], [("月度迟到 连续扣分 2 个月", -5, "expense")])
        self.assertEqual(payouts[0]["date"], app.period_end("month", app.period_shift("month", 1, this_month, -1)))
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
        # A deduct target counts submissions: no quantity measure and no shortfall reward.
        self.assertEqual(item["week_penalty"], 0)
        self.assertEqual(item["target_measure"], "count")
        self.assertEqual(item["count_days_once"], 0)
        self.backdate_rule(item["id"], self.monday(2), kind="deduct")

        self.deduct_on(item["id"], self.monday(1) + timedelta(days=1), times=2)
        data = self.state()
        self.assertEqual(data["total_points"], -4)
        self.assertEqual(self.settlement_rows(item["id"], "deduct"), [])

        self.deduct_on(item["id"], self.monday(1) + timedelta(days=2))
        charges = [row for row in self.state()["records"] if row["source"] == "over_cap_penalty"]
        self.assertEqual(
            [(row["title"], row["amount"], row["type"], row["date"]) for row in charges],
            [("看电视 超上限 1 次", -3, "expense", (self.monday(1) + timedelta(days=6)).isoformat())],
        )
        self.assertEqual(
            [(row["week_start"], row["bonus"], row["penalty"]) for row in self.settlement_rows(item["id"], "deduct")],
            [(self.monday(1).isoformat(), 0, 3)],
        )
        self.assertEqual(self.state()["total_points"], -9)

        # Grading a closed window does not lock it: a later entry inside it recomputes the charge.
        self.deduct_on(item["id"], self.monday(1) + timedelta(days=3))
        charges = [row for row in self.state()["records"] if row["source"] == "over_cap_penalty"]
        self.assertEqual(
            [(row["title"], row["amount"]) for row in charges],
            [("看电视 超上限 2 次", -6)],
        )
        self.assertEqual(self.state()["total_points"], -14)
        self.client.delete(f"/api/items/deduct/{item['id']}")

    def test_earn_streak_penalizes_consecutive_missed_windows(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.client.post(
            "/api/items/earn",
            json={
                "name": "连续缺练",
                "points": 5,
                "icon": "book.svg",
                "week_target": 2,
                "streak_weeks": 2,
                "streak_bonus": -5,
            },
        ).get_json()
        self.assertEqual((item["streak_weeks"], item["streak_bonus"]), (2, -5))
        self.assertEqual(
            self.client.post(
                "/api/items/earn",
                json={"name": "缺目标", "points": 5, "icon": "book.svg", "streak_weeks": 2, "streak_bonus": -5},
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                "/api/items/earn",
                json={"name": "缺期数", "points": 5, "icon": "book.svg", "week_target": 2, "streak_bonus": -5},
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
        # One grant in each of the last two weeks misses the two-per-week target both times.
        self.grant(item["id"], weeks_back=2)
        self.grant(item["id"], weeks_back=1)
        payouts = self.streak_records("target_streak")
        self.assertEqual(
            [(row["title"], row["amount"], row["type"]) for row in payouts],
            [("连续缺练 连续未达标 2 期", -5, "expense")],
        )
        self.assertEqual(self.state()["total_points"], 2 * 5 - 5)
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_weekly_rule_validation_and_deactivation(self):
        child = self.create_child()
        self.client.post("/api/auth/select-child", json={"child_id": child["id"]})
        item = self.client.post(
            "/api/items/earn", json={"name": "停用项目", "points": 8, "icon": "book.svg"}
        ).get_json()
        self.assertTrue(item["active"])

        response = self.client.put(
            f"/api/items/earn/{item['id']}",
            json={"name": "停用项目", "points": 8, "icon": "book.svg", "week_target": 3, "week_bonus": 2},
        )
        self.assertEqual(response.get_json()["week_target"], 3)
        response = self.client.put(
            f"/api/items/earn/{item['id']}",
            json={"name": "停用项目", "points": 8, "icon": "book.svg", "week_target": 3, "streak_bonus": 5},
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.put(
            f"/api/items/earn/{item['id']}",
            json={"name": "停用项目", "points": 8, "icon": "book.svg", "week_target": -1},
        )
        self.assertEqual(response.status_code, 400)

        response = self.client.post(f"/api/items/earn/{item['id']}/toggle")
        library = next(row for row in response.get_json()["item_library"]["earn"] if row["id"] == item["id"])
        self.assertFalse(library["active"])
        self.assertNotIn(item["id"], [row["id"] for row in self.state()["earn_items"]])
        response = self.client.post("/api/transactions", json={"kind": "earn", "item_id": item["id"]})
        self.assertEqual(response.status_code, 400)
        self.assertIn("已停用", response.get_json()["error"])

        self.client.post(f"/api/items/earn/{item['id']}/toggle")
        self.assertEqual(self.client.post("/api/transactions", json={"kind": "earn", "item_id": item["id"]}).status_code, 201)
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
        self.assertEqual(row["today_count"], 2)
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
        # Below the 10-item target nothing pays, so the closed window stays open for later logs.
        self.assertEqual([row for row in self.state()["records"] if row["source"] == "weekly_bonus"], [])
        self.assertEqual(self.submit_earn(item["id"], date=(self.monday(1) + timedelta(days=3)).isoformat(), quantity=20).status_code, 201)

        # Logging is points-free; the 27 items pay the 3 completion points plus 2 per full 5 of the 17 above target.
        self.assertEqual([(row["amount"], row["quantity"]) for row in self.earn_logs()], [(0, 20), (0, 7)])
        bonuses = [row for row in self.state()["records"] if row["source"] == "weekly_bonus"]
        self.assertEqual([(row["title"], row["amount"]) for row in bonuses], [("计算题 数量 27/10", 9)])
        self.assertEqual(self.state()["total_points"], 9)
        self.assertEqual(len(self.state()["records"]), len(bonuses) + 2)

        # Points-free logs stay out of the gain/loss lists; only the settled bonus shows.
        flows = self.client.get("/api/analytics?days=30").get_json()
        self.assertEqual([row["total"] for row in flows["lost"] if row["name"] == "计算题"], [])
        self.assertEqual([row["total"] for row in flows["gained"] if row["name"] == "计算题"], [9])
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

        # Each closed window pays the 2 completion points; only the count above the target scores, so 3 adds nothing and 6 adds 2.
        bonuses = [row for row in self.state()["records"] if row["source"] == "weekly_bonus"]
        self.assertEqual(
            [(row["title"], row["amount"]) for row in bonuses],
            [("英语单词背诵 数量 6/2", 4), ("英语单词背诵 数量 3/2", 2)],
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

        penalties = [row for row in self.state()["records"] if row["source"] == "weekly_penalty"]
        self.assertEqual([(row["title"], row["amount"]) for row in penalties], [("练字 未达标 3/10", -5)])

        day = date.today().isoformat()
        self.assertEqual(self.submit_earn(item["id"], date=day, quantity=4).status_code, 201)
        row = self.earn_item_row(item["id"])
        self.assertEqual((row["today_count"], row["week_count"]), (4, 4))
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

        payouts = self.streak_records("target_streak")
        self.assertEqual([(row["title"], row["amount"]) for row in payouts], [("口算本 连续达标 2 期", 6)])
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

        # Without a project the hand-written score still applies.
        free = self.manual_entry(name="线下补偿", points=8, date=day)
        self.assertEqual(self.manual_logs(free.get_json())[0]["title"], "补积分：线下补偿")
        # A log describes something that already happened, so dates past today are refused.
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
        # The 补录 itself is points-free; the closed window pays the 3 completion points, 2 over target is one short of every 5.
        self.assertEqual(
            [(row["title"], row["amount"], row["quantity"]) for row in self.manual_logs(response.get_json())],
            [("补录：错题整理", 0, 12)],
        )
        bonuses = [row for row in response.get_json()["records"] if row["source"] == "weekly_bonus"]
        self.assertEqual([(row["title"], row["amount"]) for row in bonuses], [("错题整理 数量 12/10", 3)])
        self.assertEqual(response.get_json()["total_points"], 3)
        self.client.delete(f"/api/items/earn/{item['id']}")

    def test_manual_backdating_resettles_closed_windows(self):
        self.client.post("/api/auth/select-child", json={"child_id": self.create_child()["id"]})
        item = self.client.post(
            "/api/items/earn",
            json={"name": "阅读", "points": 5, "icon": "book.svg", "week_target": 2, "week_penalty": 3, "week_bonus": 4},
        ).get_json()
        self.backdate_rule(item["id"], self.monday(1))
        self.grant(item["id"], weeks_back=1)
        settled = self.state()
        self.assertEqual(
            [(row["title"], row["amount"]) for row in settled["records"] if row["source"] == "weekly_penalty"],
            [("阅读 未达标 1/2", -3)],
        )
        self.assertEqual(settled["total_points"], 2)

        # A back-dated 补录 reopens its window: the water mark and its fine clear, and the new tally simply meets the target.
        added = self.manual_entry(
            item_kind="earn", item_id=item["id"], date=(self.monday(1) + timedelta(days=3)).isoformat()
        )
        self.assertEqual(added.status_code, 201)
        self.assertEqual([row for row in added.get_json()["records"] if row["source"].startswith("weekly")], [])
        self.assertEqual(self.settlement_rows(item["id"]), [])
        self.assertEqual(added.get_json()["total_points"], 10)

        # One more log tips the window over the target, so the overshoot pays instead.
        added = self.manual_entry(
            item_kind="earn", item_id=item["id"], date=(self.monday(1) + timedelta(days=4)).isoformat()
        )
        bonuses = [row for row in added.get_json()["records"] if row["source"] == "weekly_bonus"]
        self.assertEqual([(row["title"], row["amount"]) for row in bonuses], [("阅读 超额 1 次", 4)])
        self.assertEqual(added.get_json()["total_points"], 19)

        # Undoing that 补录 rolls the window back: the overshoot payout goes with it.
        undone = self.client.post(f"/api/transactions/{added.get_json()['record_id']}/undo")
        self.assertEqual(undone.status_code, 200)
        self.assertEqual([row for row in undone.get_json()["records"] if row["source"].startswith("weekly")], [])
        self.assertEqual(self.settlement_rows(item["id"]), [])
        self.assertEqual(undone.get_json()["total_points"], 10)
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
        charges = [row for row in second.get_json()["records"] if row["source"] == "over_cap_penalty"]
        self.assertEqual([(row["title"], row["amount"]) for row in charges], [("发脾气 超上限 1 次", -2)])
        self.assertEqual(second.get_json()["total_points"], -10)
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
        self.assertIn("完成作业", [row["name"] for row in data["gained"]])
        self.assertIn("临时扣分", [row["name"] for row in data["lost"]])

        scoped = self.client.get(f"/api/analytics?days=7&account_id={first['id']}").get_json()
        self.assertEqual(scoped["summary"]["gained"], 10)
        self.assertEqual({row["account_id"] for row in scoped["comparison"]}, {first["id"]})
        self.assertEqual(self.client.get("/api/analytics?days=7&account_id=invalid").status_code, 400)

        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "kid-c", "password": "child123"})
        child_data = self.client.get("/api/analytics?days=30&account_id=%s" % second["id"]).get_json()
        self.assertEqual([row["account_id"] for row in child_data["series"]], [first["id"]])

    def test_recent_flow_lists_gains_and_losses(self):
        child = self.create_child()
        self.client.post("/api/transactions", json={"kind": "earn", "points": 12, "name": "整理房间"})
        self.client.post("/api/transactions", json={"kind": "deduct", "points": 4, "name": "迟到"})
        flow = self.state()["recent_flow"]
        self.assertEqual(flow["gained"], 12)
        self.assertEqual(flow["lost"], 4)
        self.assertEqual(flow["net"], 8)
        self.assertEqual(flow["top_gained"][0]["name"], "整理房间")
        self.assertEqual(flow["top_lost"][0]["name"], "迟到")

    def test_custom_image_routes_find_uploaded_assets(self):
        for asset in ("child-boy", "child-girl", "adult-male", "adult-female"):
            response = self.client.get(f"/custom-assets/{asset}")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.content_type.startswith("image/"))
            response.close()


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


if __name__ == "__main__":
    unittest.main()
