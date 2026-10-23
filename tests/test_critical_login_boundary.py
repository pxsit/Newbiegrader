"""Real HTTP/session boundary for the public landing and member-only pages."""
from contextlib import ExitStack
import copy
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware

from critical_helpers import synthetic_problem, test_server
from fake_supabase import FakeSupabase

server = test_server()
EMAIL = "login-boundary@example.invalid"
PASSWORD = "synthetic-login-boundary-password"


class LoginBoundary(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.db = FakeSupabase()
        self.problem = synthetic_problem()
        self.db.rows("users").append({"email": EMAIL, "name": "Synthetic student",
            "password_hash": server._hash_password(PASSWORD), "auth_version": 1,
            "email_verified_at": "2026-10-23T00:00:00Z"})
        for name, value in {"_sb": self.db, "PROBLEMS": {self.problem.pid: self.problem},
            "_cache": {}, "_login_tries": {}, "_account_tries": {},
            "_contest_pid_cache": {"at": 0, "all": set(), "hide": set()}}.items():
            self.stack.enter_context(patch.object(server, name, value))
        self.stack.enter_context(patch.object(server, "_ensure_problems_fresh"))
        self.client = TestClient(server.app, base_url="https://testserver")
        self.addCleanup(self.client.close)

    def login(self):
        response = self.client.post("/api/auth/password", headers={"Origin": server.FRONTEND_URL},
            json={"email": EMAIL, "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)

    def test_critical_anonymous_cannot_read_lists_details_samples_pdfs_or_member_pages(self):
        slug = self.problem.pid
        paths = ["/api/problems", f"/api/problems/{slug}",
            f"/api/problems/{slug}/sample/01", f"/api/problems/{slug}/pdf",
            "/api/contests", "/api/contests/synthetic", "/api/history", "/api/history/synthetic",
            "/api/reports", "/api/stats", "/api/jobs/synthetic", "/api/profile",
            "/api/admin/overview", "/docs", "/redoc", "/openapi.json"]
        with patch.object(server, "_meta_or_404", side_effect=AssertionError("must deny before loading")):
            for path in paths:
                with self.subTest(path=path):
                    response = self.client.get(path, headers={"If-None-Match": '"previously-opened"'})
                    self.assertEqual(response.status_code, 401, response.text)
                    self.assertIn("no-store", response.headers["cache-control"])
                    self.assertNotIn("statement", response.text)

    def test_critical_login_allows_catalog_then_logout_denies_its_previous_cookie(self):
        self.login()
        cookie = self.client.cookies.get("ng_session")
        response = self.client.get("/api/problems")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(self.problem.pid, [p["id"] for p in response.json()["problems"]])
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(self.client.post("/api/auth/logout",
            headers={"Origin": server.FRONTEND_URL}).status_code, 200)
        response = TestClient(server.app, base_url="https://testserver").get("/api/problems",
            headers={"Cookie": "ng_session=" + cookie})
        self.assertEqual(response.status_code, 401)

    def test_critical_public_home_is_bounded_and_never_exposes_hidden_or_statement_data(self):
        problems = {}
        for i in range(10):
            p = copy.copy(self.problem)
            p.pid = f"preview_{i}"
            p.no = i + 1
            p.difficulty = "easy"
            problems[p.pid] = p
        hidden = copy.copy(self.problem)
        hidden.pid = "PRIVATE_HIDDEN_CANARY"
        hidden.hidden = True
        problems[hidden.pid] = hidden
        with patch.object(server, "PROBLEMS", problems):
            response = self.client.get("/api/home")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(len(payload["problems"]), 3)
        self.assertEqual(payload["problems_count"], 10)
        self.assertNotIn(hidden.pid, response.text)
        allowed = {"id", "no", "title", "difficulty", "tags", "time_limit", "memory_limit"}
        self.assertTrue(all(set(p) == allowed for p in payload["problems"]))
        self.assertNotIn("stats", payload)
        self.assertEqual(set(payload["summary"]), {"subs", "accepted"})

    def test_critical_health_and_identity_remain_available_to_external_observer(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        identity = self.client.get("/api/auth/me")
        self.assertEqual(identity.status_code, 200)
        self.assertIsNone(identity.json()["user"])
        self.assertEqual(self.client.get("/api/languages").status_code, 200)

    def test_critical_session_database_outage_never_opens_protected_routes(self):
        self.login()
        with patch.object(self.db, "rpc", side_effect=RuntimeError("PRIVATE_DB_ERROR")):
            response = self.client.get("/api/problems")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("PRIVATE_DB_ERROR", response.text)

    def test_critical_valid_cors_preflight_does_not_require_browser_cookies(self):
        cors = CORSMiddleware(server.app, allow_origins=[server.FRONTEND_URL],
            allow_methods=["GET"], allow_credentials=True)
        response = TestClient(cors, base_url="https://testserver").options("/api/problems", headers={
            "Origin": server.FRONTEND_URL, "Access-Control-Request-Method": "GET"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["access-control-allow-origin"], server.FRONTEND_URL)
