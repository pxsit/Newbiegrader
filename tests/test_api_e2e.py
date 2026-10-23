"""End-to-end API tests: judge queue, feedback policy, quota, contests, admin.

Runs the real FastAPI app with the real judge engine; only Supabase is replaced
by tests/fake_supabase.py, so these tests never touch production data.

    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import patch
import datetime as dt
import os
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

# Configure the app BEFORE importing it: no database, no HTTPS-only cookies,
# fixed session secret, and one test admin.
os.environ["SESSION_SECRET"] = "test-secret-not-for-production"
os.environ["SESSION_SECURE"] = "false"
os.environ["SESSION_SAMESITE"] = "lax"
os.environ["ADMIN_EMAILS"] = "admin@example.com"
os.environ["SUPABASE_URL"] = ""
os.environ["SUPABASE_KEY"] = ""
os.environ["GRADER_JUDGE_WORKERS"] = "2"

from fastapi.testclient import TestClient  # noqa: E402

from fake_supabase import FakeSupabase  # noqa: E402

import grader  # noqa: E402
import server  # noqa: E402

# Problem statements and test fixtures are NOT part of the repository: the
# source of truth is Supabase. These end-to-end tests exercise the real judge
# against a local problem set, so they only run where problems/ exists
# (a developer checkout); in CI the module is reported as skipped.
if not grader.PROBLEMS_DIR.is_dir():
    raise unittest.SkipTest(
        "no local problems/ (problem data lives in Supabase); "
        "run from a checkout that has problems/ for full API coverage")

STUDENT_A = "student.a@example.com"
STUDENT_B = "student.b@example.com"
ADMIN = "admin@example.com"
PASSWORD = "testpass123"
_thai_ref = ROOT / "examples" / "thai_time_ref.py"
THAI_REF = _thai_ref.read_text(encoding="utf-8") if _thai_ref.is_file() else ""
WRONG = 'print("nope")\n'


def _browser_origin() -> str:
    """Trusted Origin for requests that simulate a browser with a session
    cookie (the CSRF middleware requires one on unsafe methods)."""
    from urllib.parse import urlsplit
    parts = urlsplit(server.FRONTEND_URL)
    return f"{parts.scheme}://{parts.hostname}"


BROWSER = {"Origin": _browser_origin()}


def _iso(offset_minutes: int) -> str:
    return (dt.datetime.now(dt.timezone.utc)
            + dt.timedelta(minutes=offset_minutes)).isoformat()


class ApiE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._ctx = TestClient(server.app)
        cls.client = cls._ctx.__enter__()
        cls.fake = FakeSupabase()
        cls._auth_stack = ExitStack()
        cls._auth_stack.enter_context(patch.dict(os.environ, {"SUPABASE_AUTH_KEY": "synthetic-public"}))
        cls._auth_stack.enter_context(patch.object(server, "_sb_url", "https://synthetic.supabase.co"))
        cls._auth_stack.enter_context(patch.object(server.email_auth, "request_code"))
        server._sb = cls.fake
        cls._defaults = {
            "MEMORY_CAP_MB": server.MEMORY_CAP_MB,
            "QUEUE_MAX": server.QUEUE_MAX,
            "SUBMIT_QUOTA": server.SUBMIT_QUOTA,
            "ACCOUNT_MAX_TRIES": server.ACCOUNT_MAX_TRIES,
            "LOGIN_MAX_TRIES": server.LOGIN_MAX_TRIES,
        }

    @classmethod
    def tearDownClass(cls):
        server._sb = None
        cls._ctx.__exit__(None, None, None)
        cls._auth_stack.close()

    # ---------- helpers ----------
    def setUp(self):
        # Cookies persist on the shared TestClient: without this, an admin
        # login from the previous test leaks into the next one (order-dependent
        # failures, e.g. health seeing hidden problems as admin).
        self.client.cookies.clear()
        # The API rate-limits auth; the suite logs in many times from the same
        # fake IP, so start each test with clean windows.
        server._login_tries.clear()
        server._account_tries.clear()
        server._quota_hits.clear()
        server._cache.clear()
        server._contest_pid_cache["at"] = 0.0
        server._contest_pid_cache["all"] = set()
        server._contest_pid_cache["hide"] = set()
        server._reg_count_cache["at"] = 0.0
        # contests/registrations are per-test fixtures (submissions accumulate and
        # some tests assert on the running totals)
        self.fake.tables["contests"] = []
        self.fake.tables["contest_registrations"] = []
        # Restore module-level state mutated by individual tests
        server.MEMORY_CAP_MB = type(self)._defaults["MEMORY_CAP_MB"]
        server.QUEUE_MAX = type(self)._defaults["QUEUE_MAX"]
        server.SUBMIT_QUOTA = type(self)._defaults["SUBMIT_QUOTA"]
        server.ACCOUNT_MAX_TRIES = type(self)._defaults["ACCOUNT_MAX_TRIES"]
        server.LOGIN_MAX_TRIES = type(self)._defaults["LOGIN_MAX_TRIES"]
        for p in server.PROBLEMS.values():
            p.hidden = False

    def login(self, email: str, name: str = "Tester") -> None:
        self.client.cookies.clear()
        if not any(u.get("email") == email for u in self.fake.rows("users")):
            self.fake.table("users").insert({
                "email": email, "name": name, "password_hash": server._hash_password(PASSWORD),
                "email_verified_at": "2026-10-18T00:00:00+00:00", "auth_version": 1}).execute()
        r = self.client.post("/api/auth/password",
                             json={"email": email, "password": PASSWORD}, headers=BROWSER)
        self.assertEqual(r.status_code, 200, r.text)

    def submit(self, problem: str, code: str, expect: int = 202,
               contest: str = "", language: str = "python"):
        data = {"problem": problem, "language": language, "code": code}
        if contest:
            data["contest"] = contest
        r = self.client.post("/api/submit", data=data, headers=BROWSER)
        self.assertEqual(r.status_code, expect, r.text)
        return r.json() if r.content else {}

    def wait_job(self, jid: str, timeout: float = 120.0) -> dict:
        deadline = time.time() + timeout
        last = {}
        while time.time() < deadline:
            r = self.client.get(f"/api/jobs/{jid}")
            self.assertEqual(r.status_code, 200, r.text)
            last = r.json()
            if last["status"] in ("done", "error"):
                return last
            time.sleep(0.05)
        self.fail(f"job {jid} did not finish: {last}")

    def judged(self, problem: str, code: str) -> dict:
        """Submit and wait; returns the finished job payload."""
        job = self.submit(problem, code)
        self.assertEqual(job["status"], "queued", job)
        return self.wait_job(job["job_id"])

    def stored(self, **match) -> list:
        return [r for r in self.fake.rows("submissions")
                if all(r.get(k) == v for k, v in match.items())]

    # ---------- tests ----------
    def test_version_reports_test_hashes(self):
        r = self.client.get("/api/version")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["version"], grader.GRADER_VERSION)
        thai = body["tests"]["thai_time"]
        self.assertEqual(thai["hash"], grader.tests_hash(server.PROBLEMS["thai_time"]))
        self.assertEqual(thai["samples"], ["01", "02", "03"])
        self.assertGreater(thai["tests"], len(thai["samples"]))

    def test_version_reports_host_capacity(self):
        sysinfo = self.client.get("/api/version").json()["system"]
        self.assertGreater(sysinfo["cpu_quota"], 0)
        self.assertGreater(sysinfo["host_memory_mb"], 0)
        self.assertGreaterEqual(sysinfo["judge_workers"], 1)
        self.assertGreaterEqual(sysinfo["judge_jobs"], 1)
        self.assertGreaterEqual(sysinfo["memory_cap_mb"], 64)
        self.assertLessEqual(sysinfo["memory_cap_mb"], sysinfo["host_memory_mb"])
        # the memory policy is part of the judging contract: interpreted languages
        # are judged against an allowance, compiled ones stay at the declared limit
        self.assertGreater(sysinfo["mem_allowance"]["python"], 1)
        self.assertEqual(sysinfo["mem_allowance"]["cpp"], 1)
        self.assertGreater(sysinfo["mem_headroom_mb"]["python"], 0)
        self.assertGreater(sysinfo["overall_timeout_s"], 0)

    def test_memory_cap_is_applied_and_reported(self):
        # Pretend the host only has room for 64 MB per test, as on the 0.5 GB box.
        original = server.MEMORY_CAP_MB
        server.MEMORY_CAP_MB = 64
        try:
            self.login(STUDENT_A)
            job = self.judged("thai_time", THAI_REF)   # problem declares 256 MB
            result = job["result"]
            self.assertEqual(result["limits"]["memory_mb"], 64)
            self.assertEqual(result["limits"]["declared_mb"], 256)
            self.assertTrue(result["limits"]["capped"])
            self.assertTrue(result["memory_capped"])
            self.assertTrue(result["accepted"], "a tiny Python solution must still pass at 64 MB")
            row = self.stored(job_id=job["id"])[0]
            self.assertEqual(row["results"] and row["verdict"], "AC")
        finally:
            server.MEMORY_CAP_MB = original

    def test_submit_sheds_load_when_the_queue_is_full(self):
        original = server.QUEUE_MAX
        server.QUEUE_MAX = 0        # any depth counts as full
        try:
            self.login("queuefull@example.com")
            r = self.client.post("/api/submit", data={
                "problem": "thai_time", "language": "python", "code": THAI_REF},
                headers=BROWSER)
            self.assertEqual(r.status_code, 503, r.text)
            self.assertIn("queue", r.json()["detail"])
            self.assertEqual(r.headers.get("Retry-After"), "30")
        finally:
            server.QUEUE_MAX = original

    def test_samples_are_public_hidden_tests_are_not(self):
        self.login(STUDENT_A)
        for name in ("01", "02", "03"):
            ok = self.client.get(f"/api/problems/thai_time/sample/{name}")
            self.assertEqual(ok.status_code, 200, name)
            self.assertIn("input", ok.json())
        for hidden_name in ("1001", "1005", "1011", "09", "15"):
            r = self.client.get(f"/api/problems/thai_time/sample/{hidden_name}")
            self.assertEqual(r.status_code, 404, hidden_name)

    def test_accepted_submission_hides_hidden_test_data(self):
        self.login(STUDENT_A)
        job = self.judged("thai_time", THAI_REF)
        self.assertEqual(job["status"], "done", job)
        result = job["result"]
        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["score"], 100)
        self.assertEqual(result["samples"], 3)

        public = [i for i in result["results"] if not i.get("hidden")]
        hidden = [i for i in result["results"] if i.get("hidden")]
        self.assertEqual([i["test"] for i in public], ["01", "02", "03"])
        self.assertEqual(public[0]["verdict"], "AC")
        self.assertEqual(public[0]["wall_s"] >= 0, True)
        self.assertTrue(hidden, "expected hidden tests to be redacted")
        for item in hidden:
            for leaked in ("input", "expected", "actual", "diff"):
                self.assertNotIn(leaked, item, f"{item['test']} leaked {leaked}")

        rows = self.stored(job_id=job["id"])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["tests_hash"], grader.tests_hash(server.PROBLEMS["thai_time"]))
        self.assertEqual(row["grader_version"], grader.GRADER_VERSION)
        self.assertEqual(row["kind"], "submit")
        for item in row["results"]:
            if item.get("hidden"):
                self.assertNotIn("input", item)
                self.assertNotIn("expected", item)
    def test_wrong_practice_answer_keeps_failed_case_feedback(self):
        self.login(STUDENT_A)
        job = self.judged("thai_time", WRONG)
        result = job["result"]
        self.assertFalse(result["accepted"])
        self.assertEqual(result["score"], 0)
        sample = [i for i in result["results"] if i["test"] == "02"][0]
        self.assertEqual(sample["verdict"], "WA")
        self.assertIn("diff", sample)          # public sample: full feedback
        for item in result["results"]:
            if item["verdict"] != "AC":
                self.assertTrue(item.get("practice_feedback"), item)
                self.assertIn("input", item)
                self.assertIn("expected", item)
                self.assertIn("actual", item)
                self.assertFalse(item.get("hidden"), item)
            elif item.get("hidden"):
                self.assertNotIn("input", item)
                self.assertNotIn("expected", item)

    def test_submit_returns_202_and_rejects_concurrent(self):
        self.login(STUDENT_A)
        slow = "import time\ntime.sleep(1.2)\nprint('x')\n"
        first = self.submit("thai_time", slow)
        second_resp = self.client.post("/api/submit", data={
            "problem": "thai_time", "language": "python", "code": slow},
            headers=BROWSER)
        self.assertEqual(second_resp.status_code, 409, second_resp.text)
        second = second_resp.json()
        self.assertIn("already have a submission", second["detail"])
        self.assertIn("Retry-After", second_resp.headers)
        job = self.wait_job(first["job_id"])
        self.assertEqual(job["status"], "done")

    def test_submit_quota_returns_429(self):
        original = server.SUBMIT_QUOTA
        server.SUBMIT_QUOTA = (1, 600)
        try:
            self.login("quota@example.com")
            self.judged("thai_time", THAI_REF)
            r = self.client.post("/api/submit", data={
                "problem": "thai_time", "language": "python", "code": THAI_REF},
                headers=BROWSER)
            self.assertEqual(r.status_code, 429, r.text)
            self.assertIn("quota", r.json()["detail"])
            self.assertIn("Retry-After", r.headers)
        finally:
            server.SUBMIT_QUOTA = original

    def test_playground_run_with_custom_input(self):
        self.login(STUDENT_A)
        code = "import sys\nsys.stdout.write(sys.stdin.read().strip().upper())\n"
        r = self.client.post("/api/run", data={
            "language": "python", "code": code, "input": "abc", "problem": "thai_time"},
            headers=BROWSER)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["verdict"], "OK")
        self.assertEqual(body["stdout"], "ABC")

        tle = self.client.post("/api/run", data={
            "language": "python", "code": "while True:\n    pass\n",
            "input": "", "time_limit": "0.2"}, headers=BROWSER)
        self.assertEqual(tle.status_code, 200, tle.text)
        self.assertEqual(tle.json()["verdict"], "TLE")

        bad = self.client.post("/api/run", data={
            "language": "python", "code": "print(1)\n", "input": "x" * 1024},
            headers=BROWSER)
        self.assertEqual(bad.status_code, 200)
        self.assertEqual(len(bad.json()["stdout"].strip()), 1)

    def test_sanitize_compile_error_redacts_compiler_banners(self):
        from server import _sanitize_compile_error
        raw = ("gcc version 9.4.0\n"
               "g++: error: expected declaration\n"
               "javac 11.0.12\n"
               "rustc 1.70.0\n"
               "node v18.0.0\n"
               "go version go1.21\n"
               "java 17.0.8\n")
        clean = _sanitize_compile_error(raw)
        self.assertIn("gcc (compiler)", clean)
        self.assertIn("g++ (compiler)", clean)
        self.assertIn("javac (compiler)", clean)
        self.assertIn("rustc (compiler)", clean)
        self.assertIn("node (compiler)", clean)
        self.assertIn("go (compiler)", clean)
        self.assertIn("java (compiler)", clean)
        for tool in ("gcc", "g++", "javac", "rustc", "node", "go", "java"):
            # banner lines replaced; no version numbers remain on tool lines
            pass

    def test_sanitize_compile_error_truncates_long_output(self):
        from server import _sanitize_compile_error
        raw = "x" * 5000
        clean = _sanitize_compile_error(raw)
        self.assertEqual(len(clean), 4000)

    def test_sanitize_compile_error_preserves_plain_text(self):
        from server import _sanitize_compile_error
        raw = "error: expected ';' before '}' token\n    return 0;\n           ^\n"
        clean = _sanitize_compile_error(raw)
        self.assertEqual(clean, raw)


    def test_run_compile_error_sanitizes_host_paths(self):
        """Compile error via /api/run must not leak raw host paths in stderr."""
        self.login(STUDENT_A)
        bad_cpp = "int main() { return ; }\n"
        r = self.client.post("/api/run", data={
            "language": "cpp", "code": bad_cpp, "input": "", "problem": "thai_time"},
            headers=BROWSER)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["verdict"], "CE")
        stderr = body.get("stderr", "")
        self.assertIn("[path]", stderr)
        for prefix in ("/tmp", "/app", "/root", "/home", "/usr", "/var", "/etc"):
            self.assertNotIn(prefix, stderr,
                             f"raw host path prefix leaked into compile error: {prefix!r}")


    def test_sanitize_compile_error_redacts_host_paths(self):
        """_sanitize_compile_error must never return raw host paths to clients."""
        from server import _sanitize_compile_error
        raw = ("clang++: /tmp/workdir/solution.cpp:5:10: error: expected ';' after expression\n"
               "    return ;\n"
               "           ^\n"
               "/usr/include/c++/14/ostream: note: candidate function\n"
               "gcc version 14.2.0\n")
        clean = _sanitize_compile_error(raw)
        for prefix in ("/tmp", "/app", "/root", "/home", "/usr", "/var", "/etc"):
            self.assertNotIn(prefix, clean,
                             f"raw host path prefix leaked: {prefix!r}")
        self.assertIn("[path]", clean)
        self.assertIn("clang++: [path]", clean)
        self.assertIn("(compiler)", clean)

    def test_delete_rejects_unknown_filter_column(self):
        """fake_supabase delete must validate filter column keys against whitelist."""
        from fake_supabase import FakeSupabase
        db = FakeSupabase(columns={"submissions": {"id", "user_email", "problem"}})
        db.table("submissions").insert({"user_email": "a@example.com", "problem": "p1"}).execute()
        with self.assertRaises(Exception) as ctx:
            db.table("submissions").delete().eq("nonexistent_col", 1).execute()
        self.assertIn("does not exist", str(ctx.exception))
        # known column still works
        db.table("submissions").delete().eq("problem", "p1").execute()
        self.assertEqual(len(db.tables["submissions"]), 0)

    def test_history_detail_allows_logged_in_peer_and_keeps_owner_admin_access(self):
        self.login(STUDENT_A)
        job = self.judged("thai_time", THAI_REF)
        sid = job["result"]["id"]
        self.assertTrue(sid)

        # Learning access: a signed-in student may read a peer's completed
        # source without first qualifying on the problem.
        self.login(STUDENT_B)
        r = self.client.get(f"/api/history/{sid}")
        self.assertEqual(r.status_code, 200, r.text)
        peer = r.json()
        self.assertTrue(peer["peer_inspection"])
        self.assertIn("code", peer)
        self.assertIn("THAI", peer["code"].upper() + "THAI")
        for test in peer["results"]:
            self.assertTrue(test["hidden"])
            self.assertNotIn("input", test)
            self.assertNotIn("expected", test)
            self.assertNotIn("actual", test)

        self.login(STUDENT_A)
        r = self.client.get(f"/api/history/{sid}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("code", r.json())
        self.assertIn("THAI", r.json()["code"].upper() + "THAI")

        self.login(ADMIN)
        r = self.client.get(f"/api/history/{sid}")
        self.assertEqual(r.status_code, 200, r.text)

    def test_contests_end_to_end(self):
        self.login(ADMIN)
        r = self.client.post("/api/contests", json={
            "id": "test-cup", "title": "Test Cup",
            "starts_at": _iso(-5), "ends_at": _iso(55),
            "problems": ["thai_time"], "freeze_minutes": 60, "show_samples": False,
            "hide_from_tasks": False}, headers=BROWSER)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["contest"]["status"], "running")

        self.login(STUDENT_A)
        listed = self.client.get("/api/contests").json()
        self.assertTrue(listed["configured"])
        self.assertIn("test-cup", [c["id"] for c in listed["contests"]])
        self.assertEqual([c for c in listed["contests"]
                          if c["id"] == "test-cup"][0]["status"], "running")

        # running contest with show_samples=false: even sample data is withheld
        job = self.judged_contest("thai_time", WRONG, "test-cup")
        for item in job["result"]["results"]:
            self.assertNotIn("input", item, item)
            self.assertNotIn("expected", item, item)
            self.assertTrue(item.get("hidden"), item)
        rows = self.stored(job_id=job["id"])
        self.assertEqual(rows[0]["contest_id"], "test-cup")

        # problem outside the contest, and a closed contest, are refused
        self.submit("hanoi", THAI_REF, expect=400, contest="test-cup")

        detail = self.client.get("/api/contests/test-cup").json()
        self.assertEqual(detail["id"], "test-cup")
        self.assertTrue(detail["frozen"], "freeze window must hide live rows")
        self.assertEqual(detail["scoreboard"], [], "frozen board hides fresh rows")
        self.assertEqual([p["id"] for p in detail["problem_details"]], ["thai_time"])

        self.login(ADMIN)
        admin_view = self.client.get("/api/contests/test-cup").json()
        self.assertFalse(admin_view["frozen"])
        self.assertEqual(admin_view["scoreboard"][0]["name"], "Tester")

        past = self.client.post("/api/contests", json={
            "id": "old-cup", "title": "Old", "starts_at": _iso(-120),
            "ends_at": _iso(-60), "problems": ["thai_time"]}, headers=BROWSER)
        self.assertEqual(past.status_code, 200)
        self.logout_in(STUDENT_A)
        self.submit("thai_time", THAI_REF, expect=403, contest="old-cup")

        self.login(ADMIN)
        self.assertEqual(self.client.delete("/api/contests/old-cup",
                                            headers=BROWSER).status_code, 200)
        bad = self.client.post("/api/contests", json={
            "id": "bad cup", "starts_at": _iso(0), "ends_at": _iso(10),
            "problems": ["thai_time"]}, headers=BROWSER)
        self.assertEqual(bad.status_code, 400)

    def register_contest(self, cid: str) -> dict:
        r = self.client.post(f"/api/contests/{cid}/register", json={},
                             headers=BROWSER)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def judged_contest(self, problem: str, code: str, contest: str) -> dict:
        self.register_contest(contest)     # contest submissions need registration
        job = self.submit(problem, code, contest=contest)
        return self.wait_job(job["job_id"])

    def logout_in(self, email: str) -> None:
        self.login(email)

    def test_rejudge_updates_the_same_row(self):
        self.login(STUDENT_A)
        job = self.judged("thai_time", THAI_REF)
        sid = job["result"]["id"]
        before = len(self.fake.rows("submissions"))

        self.login(ADMIN)
        r = self.client.post("/api/admin/rejudge", json={"ids": [sid]},
                             headers=BROWSER)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["queued"], 1)
        done = self.wait_job(r.json()["jobs"][0])
        self.assertEqual(done["status"], "done", done)

        self.assertEqual(len(self.fake.rows("submissions")), before,
                         "rejudge must not create a new row")
        rows = self.stored(id=sid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "rejudge")
        self.assertEqual(rows[0]["passed"] + sum(
            n for v, n in rows[0]["counts"].items() if v != "AC"), rows[0]["total"])

    def test_plagiarism_finds_identical_submissions(self):
        self.login(STUDENT_A)
        first = self.judged("thai_time", THAI_REF)["result"]["id"]
        self.login(STUDENT_B)
        second = self.judged("thai_time", THAI_REF)["result"]["id"]

        self.login(ADMIN)
        r = self.client.get("/api/admin/plagiarism",
                            params={"problem": "thai_time", "threshold": 0.8})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertGreaterEqual(body["scanned"], 2)
        mine = [p for p in body["pairs"]
                if {p["a_id"], p["b_id"]} == {first, second}]
        self.assertTrue(mine, f"pair for the two identical submissions missing: {body['pairs']}")
        self.assertGreaterEqual(mine[0]["similarity"], 0.8)
        self.assertGreater(mine[0]["matches"], 10)
        self.assertEqual(mine[0]["language"], "python")

    def test_login_limit_is_per_account_not_per_network(self):
        # Through the proxy every visitor looks like one IP, so an IP-only limit
        # locked out the whole site after 20 logins. Wrong passwords must only
        # lock the account, and a success must clear that account's counter.
        original = server.ACCOUNT_MAX_TRIES
        server.ACCOUNT_MAX_TRIES = 2
        try:
            self.login("rate.limit.a@example.com")
            self.client.cookies.clear()
            server._account_tries.clear()   # registering consumed one slot
            for _ in range(2):
                r = self.client.post("/api/auth/password", json={
                    "email": "rate.limit.a@example.com", "password": "wrongpass123"},
                    headers=BROWSER)
                self.assertEqual(r.status_code, 401, r.text)
            blocked = self.client.post("/api/auth/password", json={
                "email": "rate.limit.a@example.com", "password": "testpass123"},
                headers=BROWSER)
            self.assertEqual(blocked.status_code, 429, blocked.text)
            self.assertIn("account", blocked.json()["detail"])

            # a different student on the same (shared) network is unaffected
            self.login("rate.limit.b@example.com", "B")

            # one wrong try then a success resets the counter for that account
            server._account_tries.clear()
            self.client.post("/api/auth/password", json={
                "email": "rate.limit.a@example.com", "password": "wrongpass123"},
                headers=BROWSER)
            ok = self.client.post("/api/auth/password", json={
                "email": "rate.limit.a@example.com", "password": "testpass123"},
                headers=BROWSER)
            self.assertEqual(ok.status_code, 200, ok.text)
            self.assertNotIn("rate.limit.a@example.com", server._account_tries)
        finally:
            server.ACCOUNT_MAX_TRIES = original

    def test_forged_forwarded_headers_do_not_bypass_ip_limit(self):
        # Rotating X-Forwarded-For / CF-Connecting-IP must not dodge the
        # per-network bucket: untrusted peers resolve to the socket peer.
        original = server.LOGIN_MAX_TRIES
        server.LOGIN_MAX_TRIES = 3
        try:
            self.client.cookies.clear()
            server._login_tries.clear()
            for i in range(3):
                r = self.client.post("/api/auth/password", json={
                    "email": f"nobody{i}@example.com", "password": "wrongpass123"},
                    headers={**BROWSER,
                             "X-Forwarded-For": f"198.51.100.{i}",
                             "CF-Connecting-IP": "192.0.2.99"})
                self.assertEqual(r.status_code, 401, r.text)
            flooded = self.client.post("/api/auth/password", json={
                "email": "fresh@example.com", "password": "wrongpass123"},
                headers={**BROWSER, "X-Forwarded-For": "203.0.113.250"})
            self.assertEqual(flooded.status_code, 429, flooded.text)
            self.assertIn("network", flooded.json()["detail"])
            # Forged identities never entered the rate-limit map at all.
            self.assertNotIn("198.51.100.0", server._login_tries)
            self.assertNotIn("192.0.2.99", server._login_tries)
            self.assertNotIn("203.0.113.250", server._login_tries)
        finally:
            server.LOGIN_MAX_TRIES = original

    def test_admin_endpoints_forbidden_for_students(self):
        self.login(STUDENT_A)
        self.assertEqual(self.client.post("/api/admin/rejudge",
                                          json={"ids": []},
                                          headers=BROWSER).status_code, 403)
        self.assertEqual(self.client.get("/api/admin/plagiarism",
                                         params={"problem": "thai_time"}).status_code, 403)
        self.assertEqual(self.client.post("/api/contests", json={},
                                          headers=BROWSER).status_code, 403)
        self.assertEqual(self.client.get("/api/admin/overview").status_code, 403)

    def test_security_regressions(self):
        # Admin addresses are reserved: open registration must refuse them.
        # (Browser Origin attached so the route itself answers, not CSRF.)
        r = self.client.post("/api/auth/register",
                             json={"email": ADMIN, "password": PASSWORD, "name": "X"},
                             headers=BROWSER)
        self.assertEqual(r.status_code, 403, r.text)
        # Unknown API paths require login and never return the SPA index.html.
        r = self.client.get("/api/no-such-endpoint")
        self.assertEqual(r.status_code, 401, r.text)
        self.assertIn("application/json", r.headers.get("content-type", ""))
        # Bad plagiarism params are 400/422, not 500.
        self.login(ADMIN)
        r = self.client.get("/api/admin/plagiarism",
                            params={"problem": "thai_time", "threshold": "abc"})
        self.assertIn(r.status_code, (400, 422), r.text)
        r = self.client.get("/api/admin/plagiarism",
                            params={"problem": "thai_time", "limit": "abc"})
        self.assertIn(r.status_code, (400, 422), r.text)
        # Another user's job id is indistinguishable from unknown (404).
        # NOTE: check before creating the upcoming contest below, which hides
        # thai_time from non-admins until it starts.
        job = self.submit("thai_time", THAI_REF)
        jid = job["job_id"]
        self.login(STUDENT_B)
        r = self.client.get(f"/api/jobs/{jid}")
        self.assertEqual(r.status_code, 404, r.text)
        r = self.client.get("/api/jobs/does-not-exist")
        self.assertEqual(r.status_code, 404, r.text)
        # Upcoming contests hide problem details from non-admins.
        self.login(ADMIN)
        r = self.client.post("/api/contests", json={
            "id": "future-cup", "title": "Future",
            "starts_at": _iso(60), "ends_at": _iso(120),
            "problems": ["thai_time"]}, headers=BROWSER)
        self.assertEqual(r.status_code, 200, r.text)
        self.login(STUDENT_A)
        detail = self.client.get("/api/contests/future-cup").json()
        self.assertEqual(detail["status"], "upcoming")
        for p in detail["problem_details"]:
            self.assertNotIn("title", p, p)

    def test_csrf_origin_protection(self):
        # Canonical origin comes from server config, never from the request.
        from urllib.parse import urlsplit
        parts = urlsplit(server.FRONTEND_URL)
        GOOD = f"{parts.scheme}://{parts.hostname}"
        EVIL = "https://evil.example"
        SUFFIX = f"{parts.scheme}://{parts.hostname}.evil.example"
        payload = {"email": STUDENT_A, "password": PASSWORD}
        self.login(STUDENT_A)
        # 1. Same-origin POST is accepted.
        r = self.client.post("/api/auth/password", json=payload,
                             headers={"Origin": GOOD})
        self.assertEqual(r.status_code, 200, r.text)
        # 2. Cross-origin Origin is rejected with 403 (generic detail only).
        r = self.client.post("/api/auth/password", json=payload,
                             headers={"Origin": EVIL})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json(), {"detail": "cross-origin request refused"})
        # 3. Valid same-origin Referer works when Origin is absent.
        r = self.client.post("/api/auth/password", json=payload,
                             headers={"Referer": GOOD + "/#/tasks"})
        self.assertEqual(r.status_code, 200, r.text)
        # 4. Cross-origin Referer is rejected when Origin is absent.
        r = self.client.post("/api/auth/password", json=payload,
                             headers={"Referer": EVIL + "/x"})
        self.assertEqual(r.status_code, 403, r.text)
        # 5. Session cookie present but both headers absent -> 403.
        # A browser request carrying our cookie without Origin/Referer is
        # either forged or header-stripped: fail closed. (Headerless
        # non-browser clients are covered by test_csrf_allows_cookieless.)
        r = self.client.post("/api/auth/password", json=payload)
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json(), {"detail": "cross-origin request refused"})
        # 6. Malformed Origin/Referer values are rejected safely (403, no 500).
        for bad in ("not-a-url", "null", "", "https://", "ftp://x.example"):
            r = self.client.post("/api/auth/password", json=payload,
                                 headers={"Origin": bad})
            self.assertEqual(r.status_code, 403, (bad, r.text))
        r = self.client.post("/api/auth/password", json=payload,
                             headers={"Referer": "junk"})
        self.assertEqual(r.status_code, 403, r.text)
        # 7. Suffix-match hostname and scheme downgrade are not accepted.
        for bad in (SUFFIX, "http://" + parts.hostname):
            r = self.client.post("/api/auth/password", json=payload,
                                 headers={"Origin": bad})
            self.assertEqual(r.status_code, 403, (bad, r.text))
        # 8. Mismatched Origin wins even when Referer is valid.
        r = self.client.post("/api/auth/password", json=payload,
                             headers={"Origin": EVIL, "Referer": GOOD + "/"})
        self.assertEqual(r.status_code, 403, r.text)
        # 9. Admin and contest mutations are covered too.
        self.login(ADMIN)
        contest = {"id": "csrf-cup", "title": "CSRF Cup",
                   "starts_at": _iso(60), "ends_at": _iso(120),
                   "problems": ["thai_time"]}
        r = self.client.post("/api/contests", json=contest,
                             headers={"Origin": EVIL})
        self.assertEqual(r.status_code, 403, r.text)
        r = self.client.post("/api/contests", json=contest,
                             headers={"Origin": GOOD})
        self.assertEqual(r.status_code, 200, r.text)
        self.login(STUDENT_A)
        r = self.client.post("/api/contests/csrf-cup/register", json={},
                             headers={"Origin": EVIL})
        self.assertEqual(r.status_code, 403, r.text)
        r = self.client.post("/api/contests/csrf-cup/register", json={},
                             headers={"Origin": GOOD})
        self.assertEqual(r.status_code, 200, r.text)
        # 10. Google OAuth completion (GET) remains functional, not 403.
        r = self.client.get("/api/auth/finish", follow_redirects=False)
        self.assertNotEqual(r.status_code, 403, r.text)
        # 11. GET polling and read-only endpoints ignore Origin entirely.
        r = self.client.get("/api/health", headers={"Origin": EVIL})
        self.assertEqual(r.status_code, 200, r.text)

    def test_csrf_allows_cookieless_but_never_grants_access(self):
        # No session cookie + no headers -> the request passes the CSRF layer
        # and is decided by the existing authentication/authorization layers.
        self.client.cookies.clear()
        # Protected operation without credentials: auth layer refuses (401),
        # not the CSRF layer — and nothing is executed or disclosed.
        r = self.client.post("/api/submit", data={
            "problem": "thai_time", "language": "python", "code": "print(1)"})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertNotIn("cross-origin", r.text)
        # Playground likewise requires login first.
        r = self.client.post("/api/run", data={
            "language": "python", "code": "print(1)\n", "input": ""})
        self.assertEqual(r.status_code, 401, r.text)
        # ...while a genuinely new non-browser registration still proceeds.
        r = self.client.post("/api/auth/register",
                             json={"email": "cookieless@example.com",
                                   "password": PASSWORD, "name": "C"})
        self.assertEqual(r.status_code, 202, r.text)
        self.assertIsNone(self.client.get("/api/auth/me").json()["user"])

    def test_csrf_session_cookie_requires_headers_on_mutations(self):
        # Representative mutation matrix: session cookie + no headers -> 403
        # everywhere, without executing anything or leaking data.
        self.login(STUDENT_A)
        before = self.stored(user_email=STUDENT_A)
        r = self.client.post("/api/submit", data={
            "problem": "thai_time", "language": "python", "code": "print(1)"})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self.stored(user_email=STUDENT_A), before,
                         "refused submit must not create history")
        r = self.client.post("/api/run", data={
            "language": "python", "code": "print(1)\n", "input": ""})
        self.assertEqual(r.status_code, 403, r.text)
        r = self.client.put("/api/profile", json={"display_name": "X"})
        self.assertEqual(r.status_code, 403, r.text)
        self.login(ADMIN)
        r = self.client.post("/api/admin/rejudge",
                             json={"problem": "thai_time", "limit": 1})
        self.assertEqual(r.status_code, 403, r.text)
        # Same calls with a valid Origin pass the CSRF layer (route decides).
        r = self.client.post("/api/admin/rejudge",
                             json={"problem": "thai_time", "limit": 1},
                             headers=BROWSER)
        self.assertIn(r.status_code, (200, 503), r.text)


    def test_csrf_loopback_origin_rejected_in_production(self):
        # Production (SESSION_SECURE=true) must not trust Origin: http://localhost.
        # A victim browser can forge this origin to bypass CSRF on a public host;
        # loopback trust is dev-only (SESSION_SECURE=false).
        self.login(STUDENT_A)
        payload = {"email": STUDENT_A, "password": PASSWORD}

        # Production mode: loopback origin rejected with 403
        os.environ["SESSION_SECURE"] = "true"
        r = self.client.post("/api/auth/password", json=payload,
                             headers={"Origin": "http://localhost"})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json(), {"detail": "cross-origin request refused"})

        # Dev mode: loopback origin accepted (local development unchanged)
        os.environ["SESSION_SECURE"] = "false"
        r = self.client.post("/api/auth/password", json=payload,
                             headers={"Origin": "http://localhost"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_csrf_loopback_origin_127_evil_rejected(self):
        # A hostname like "127.evil.com" starts with "127." but is NOT
        # loopback. ipaddress.ip_address().is_loopback must reject it.
        self.login(STUDENT_A)
        payload = {"email": STUDENT_A, "password": PASSWORD}

        # Production mode: 127.evil.com origin rejected with 403
        os.environ["SESSION_SECURE"] = "true"
        r = self.client.post("/api/auth/password", json=payload,
                             headers={"Origin": "http://127.evil.com"})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json(), {"detail": "cross-origin request refused"})

        # Dev mode: 127.evil.com still rejected (not real loopback)
        os.environ["SESSION_SECURE"] = "false"
        r = self.client.post("/api/auth/password", json=payload,
                             headers={"Origin": "http://127.evil.com"})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json(), {"detail": "cross-origin request refused"})

    def test_admin_overview_reports_queue_state(self):

        # monitor fields for the admin banner (alert at >=5 queued).
        self.login(ADMIN)
        r = self.client.get("/api/admin/overview")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("jobs", body)
        self.assertEqual(set(body["jobs"]), {"queued", "running", "queue_depth", "workers",
                                             "queue_max", "queue_alert", "queue_full"})
        self.assertFalse(body["jobs"]["queue_alert"])
        self.assertFalse(body["jobs"]["queue_full"])
        self.assertGreaterEqual(body["jobs"]["workers"], 1)
        self.assertGreaterEqual(body["problems"], 40)
        self.assertIn("count_islands", body["system"]["problems_loaded"])

    def test_stats_use_all_time_views(self):
        self.login(STUDENT_A)
        # Independent of earlier judging tests or platform execution support.
        row = {"id": "stats-view-canary", "problem": "thai_time", "accepted": True,
               "score": 100, "user_email": STUDENT_A, "contest_id": None}
        self.fake.rows("submissions").append(row)
        self.addCleanup(lambda: self.fake.rows("submissions").remove(row))
        s = self.client.get("/api/stats").json()
        self.assertTrue(s["configured"])
        thai = s["stats"]["thai_time"]
        self.assertGreaterEqual(thai["subs"], thai["accepted"])
        self.assertGreaterEqual(thai["best"], 0)

    def test_contest_problems_are_hidden_from_the_task_list(self):
        # The admin drops in a problem set for a contest: it must vanish from the
        # public Task list but stay reachable from the contest page and admin.
        self.login(STUDENT_A)
        before = [p["id"] for p in self.client.get("/api/problems").json()["problems"]]
        self.assertIn("hanoi", before)

        self.login(ADMIN)
        created = self.client.post("/api/contests", json={
            "id": "cup-only", "title": "Cup Only",
            "starts_at": _iso(-5), "ends_at": _iso(55),
            "problems": ["hanoi"], "hide_from_tasks": True}, headers=BROWSER)
        self.assertEqual(created.status_code, 200, created.text)
        self.assertTrue(created.json()["contest"]["hide_from_tasks"])

        self.login(STUDENT_A)
        student_list = self.client.get("/api/problems").json()["problems"]
        self.assertNotIn("hanoi", [p["id"] for p in student_list])
        self.assertNotIn("hanoi", self.client.get("/api/health").json()["problems"])
        self.assertNotIn("hanoi", self.client.get("/api/version").json()["tests"])
        # ...but the contest page still opens it (that is the only way in)
        self.assertEqual(self.client.get("/api/problems/hanoi").status_code, 200)
        sample = server.PROBLEMS["hanoi"].samples[0]
        self.assertEqual(
            self.client.get(f"/api/problems/hanoi/sample/{sample}").status_code, 200)
        contest = self.client.get("/api/contests/cup-only").json()
        self.assertIn("hanoi", [p["id"] for p in contest["problem_details"]])
        job = self.judged_contest("hanoi", (ROOT / "examples" / "hanoi_ref.py").read_text(), "cup-only")
        self.assertEqual(job["result"]["score"], 100)

        self.login(ADMIN)
        admin_list = self.client.get("/api/problems").json()["problems"]
        admin_hanoi = [p for p in admin_list if p["id"] == "hanoi"][0]
        self.assertTrue(admin_hanoi["hidden"])
        self.assertTrue(admin_hanoi["contest_hidden"])
        overview = self.client.get("/api/admin/overview").json()
        self.assertIn("hanoi", [h["id"] for h in overview["problems_contest_only"]])
        self.assertEqual(
            self.client.get(f"/api/problems/hanoi/sample/{sample}").status_code, 200)

        # a contest that reuses public problems keeps them in Tasks
        self.assertEqual(self.client.post("/api/contests", json={
            "id": "cup-open", "title": "Open", "starts_at": _iso(-5), "ends_at": _iso(55),
            "problems": ["warpz"], "hide_from_tasks": False},
            headers=BROWSER).status_code, 200)
        self.login(STUDENT_A)
        self.assertIn("warpz", [p["id"] for p in
                                self.client.get("/api/problems").json()["problems"]])
        public_hist = self.client.get("/api/history?limit=50").json()["submissions"]
        self.assertNotIn("hanoi", [r["problem"] for r in public_hist])

        self.login(ADMIN)
        self.client.delete("/api/contests/cup-only", headers=BROWSER)
        self.client.delete("/api/contests/cup-open", headers=BROWSER)

    def test_config_hidden_problem_is_private(self):
        prob = server.PROBLEMS["mirror_pair"]
        original = prob.hidden
        prob.hidden = True
        try:
            self.login(STUDENT_A)
            ids = [p["id"] for p in self.client.get("/api/problems").json()["problems"]]
            self.assertNotIn("mirror_pair", ids)
            mp_sample = server.PROBLEMS["mirror_pair"].samples[0]
            for path in ("/api/problems/mirror_pair",
                         f"/api/problems/mirror_pair/sample/{mp_sample}",
                         "/api/problems/mirror_pair/pdf"):
                self.assertEqual(self.client.get(path).status_code, 404, path)
            r = self.client.post("/api/submit", data={
                "problem": "mirror_pair", "language": "python", "code": "print(1)"},
                headers=BROWSER)
            self.assertEqual(r.status_code, 404, r.text)
            self.login(ADMIN)
            self.assertEqual(self.client.get("/api/problems/mirror_pair").status_code, 200)
            self.assertIn("mirror_pair",
                          [p["id"] for p in self.client.get("/api/problems").json()["problems"]])
            self.assertIn("mirror_pair", self.client.get("/api/version").json()["hidden_problems"])
        finally:
            prob.hidden = original

    def test_rejudge_is_capped_by_queue_budget(self):
        original = server.QUEUE_MAX
        server.QUEUE_MAX = 1        # one worker, one job at a time
        try:
            self.login(STUDENT_A)
            job = self.judged("thai_time", THAI_REF)
            sid = job["result"]["id"]

            self.login(ADMIN)
            r = self.client.post("/api/admin/rejudge", json={"problem": "thai_time", "limit": 50},
                                 headers=BROWSER)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["queued"], 1, "batch must be clamped to the queue budget")
            self.wait_job(r.json()["jobs"][0])
            self.assertEqual(len(self.stored(id=sid)), 1)
        finally:
            server.QUEUE_MAX = original

    def test_problem_numbers_are_contiguous_and_resolve(self):
        # The repo numbering is an invariant (tools/number_problems.py keeps it):
        # public 1..P with no gaps, contest-only 1001.. with no gaps, and any
        # problem can be addressed by its number.
        all_nos = sorted(p.no for p in server.PROBLEMS.values())
        public = [n for n in all_nos if n < 1000]
        hidden_block = [n for n in all_nos if n >= 1000]
        self.assertEqual(public, list(range(1, len(public) + 1)), public)
        self.assertEqual(hidden_block, list(range(1001, 1001 + len(hidden_block))),
                         hidden_block)

        self.login(STUDENT_A)
        probs = self.client.get("/api/problems").json()["problems"]
        nums = [p["no"] for p in probs]
        self.assertTrue(all(n > 0 for n in nums), "every listed problem needs a number")
        self.assertEqual(nums, sorted(nums), "the list is ordered by number")
        hidden = server._hidden_pids()
        hidden_nos = {server.PROBLEMS[pid].no for pid in hidden}
        self.assertTrue(hidden_nos.isdisjoint(set(nums)), "hidden problems stay out of the list")
        self.assertEqual({p["id"] for p in probs},
                         {pid for pid in server.PROBLEMS if pid not in hidden})

        pid = server.PROBLEMS_BY_NO[19]
        detail = self.client.get("/api/problems/19").json()
        self.assertEqual(detail["id"], pid)
        self.assertEqual(detail["no"], 19)
        sample = server.PROBLEMS[pid].samples[0]
        self.assertEqual(self.client.get(f"/api/problems/19/sample/{sample}").status_code, 200)
        self.assertEqual(self.client.get("/api/problems/19/pdf").status_code,
                         200 if (ROOT / "problems" / pid / "statement.pdf").is_file() else 404)

        # submitting by number judges the same problem as by slug
        code = (ROOT / "examples" / f"{pid}_ref.py").read_text(encoding="utf-8")
        job = self.submit("19", code)
        self.assertEqual(job["problem"], pid, job)
        done = self.wait_job(job["job_id"])
        self.assertEqual(done["status"], "done", done)
        row = self.stored(id=done["result"]["id"])[0]
        self.assertEqual(row["problem"], pid)

        # contests accept numbers too and store canonical slugs
        self.login(ADMIN)
        created = self.client.post("/api/contests", json={
            "id": "num-cup", "title": "Num Cup", "starts_at": _iso(-5), "ends_at": _iso(30),
            "problems": ["19"], "hide_from_tasks": False}, headers=BROWSER)
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["contest"]["problems"], [pid])
        self.assertEqual(self.client.delete("/api/contests/num-cup",
                                            headers=BROWSER).status_code, 200)

    def test_contest_registration_locks_problems(self):
        # Codeforces lifecycle: register before the start, problems open at the
        # start, unregistered users cannot submit.
        self.addCleanup(lambda: self.fake.tables.__setitem__("contests", []))
        self.login(ADMIN)
        created = self.client.post("/api/contests", json={
            "id": "cf-style", "title": "CF Style",
            "starts_at": _iso(10), "ends_at": _iso(70),
            "problems": ["hanoi"], "hide_from_tasks": True}, headers=BROWSER)
        self.assertEqual(created.status_code, 200, created.text)

        self.login(STUDENT_A)
        d = self.client.get("/api/contests/cf-style").json()
        self.assertFalse(d["problems_open"])
        self.assertFalse(d["registered"])
        self.assertFalse(d["can_submit"])
        self.assertGreater(d["starts_in_s"], 0)
        self.assertEqual(d["participants"], 0)
        self.assertEqual(self.client.get("/api/problems/hanoi").status_code, 404)
        early = self.client.post("/api/submit", data={
            "problem": "hanoi", "language": "python",
            "code": "print(1)", "contest": "cf-style"}, headers=BROWSER)
        self.assertEqual(early.status_code, 404, early.text)   # problem not open yet
        self.client.post("/api/contests/cf-style/register", json={}, headers=BROWSER)
        still_early = self.client.post("/api/submit", data={
            "problem": "hanoi", "language": "python",
            "code": "print(1)", "contest": "cf-style"}, headers=BROWSER)
        self.assertEqual(still_early.status_code, 404, still_early.text)

        reg = self.client.post("/api/contests/cf-style/register", json={},
                               headers=BROWSER)
        self.assertEqual(reg.status_code, 200, reg.text)
        self.assertTrue(reg.json()["registered"])
        self.assertEqual(reg.json()["participants"], 1)
        self.assertTrue(self.client.get("/api/contests/cf-style").json()["registered"])
        # still locked before the start, even when registered
        self.assertEqual(self.client.get("/api/problems/hanoi").status_code, 404)
        self.assertFalse(self.client.get("/api/contests/cf-style").json()["problems_open"])

        unreg = self.client.delete("/api/contests/cf-style/register", headers=BROWSER)
        self.assertEqual(unreg.status_code, 200, unreg.text)
        self.assertFalse(unreg.json()["registered"])
        self.client.post("/api/contests/cf-style/register", json={},
                         headers=BROWSER)   # register again

        # move the window so it is running now
        self.login(ADMIN)
        now = self.client.post("/api/contests", json={
            "id": "cf-style", "title": "CF Style",
            "starts_at": _iso(-5), "ends_at": _iso(55),
            "problems": ["hanoi"], "hide_from_tasks": True}, headers=BROWSER)
        self.assertEqual(now.status_code, 200, now.text)
        self.assertEqual(now.json()["contest"]["status"], "running")
        self.assertEqual(now.json()["contest"]["participants"], 1)

        self.login(STUDENT_A)
        running = self.client.get("/api/contests/cf-style").json()
        self.assertTrue(running["problems_open"])
        self.assertTrue(running["can_submit"])
        self.assertEqual(self.client.get("/api/problems/hanoi").status_code, 200)
        code = (ROOT / "examples" / "hanoi_ref.py").read_text(encoding="utf-8")
        job = self.judged_contest("hanoi", code, "cf-style")
        self.assertEqual(job["result"]["score"], 100)

        # a classmate who never registered is refused
        self.login(STUDENT_B)
        refused = self.client.post("/api/submit", data={
            "problem": "hanoi", "language": "python", "code": code, "contest": "cf-style"},
            headers=BROWSER)
        self.assertEqual(refused.status_code, 403, refused.text)
        self.assertIn("ลงทะเบียน", refused.json()["detail"])
        self.assertFalse(self.client.get("/api/contests/cf-style").json()["can_submit"])
        # but they can register while it runs, then submit
        self.assertEqual(self.client.post(
            "/api/contests/cf-style/register", json={}, headers=BROWSER).status_code, 200)
        self.assertTrue(self.judged_contest("hanoi", code, "cf-style")["result"]["accepted"])
        self.assertEqual(self.client.delete(
            "/api/contests/cf-style/register", headers=BROWSER).status_code, 400)   # cannot leave mid-round

        # after the end: problems stay open for practice, submissions are closed
        self.login(ADMIN)
        self.client.post("/api/contests", json={
            "id": "cf-style", "title": "CF Style",
            "starts_at": _iso(-120), "ends_at": _iso(-60),
            "problems": ["hanoi"], "hide_from_tasks": True}, headers=BROWSER)
        self.login(STUDENT_A)
        after = self.client.get("/api/contests/cf-style").json()
        self.assertTrue(after["problems_open"])
        self.assertFalse(after["can_submit"])
        self.assertEqual(self.client.get("/api/problems/hanoi").status_code, 200)
        late = self.client.post("/api/submit", data={
            "problem": "hanoi", "language": "python", "code": code, "contest": "cf-style"},
            headers=BROWSER)
        self.assertEqual(late.status_code, 403, late.text)

        self.login(ADMIN)
        self.assertEqual(self.client.delete("/api/contests/cf-style",
                                            headers=BROWSER).status_code, 200)
        params = self.client.get("/api/contests/cf-style").status_code
        self.assertEqual(params, 404)

    def test_contest_hides_problems_until_start_midround_add_and_announcements(self):
        # Upcoming: no slugs anywhere for non-admins; mid-round upsert adds
        # problems live; announcements are admin-posted and start-gated.
        self.addCleanup(lambda: self.fake.tables.__setitem__("contests", []))
        self.addCleanup(lambda: self.fake.tables.__setitem__("contest_announcements", []))
        server._annc_table_ok["value"] = True
        from urllib.parse import urlsplit as _urlsplit
        _parts = _urlsplit(server.FRONTEND_URL)
        browser = {"Origin": f"{_parts.scheme}://{_parts.hostname}"}
        self.login(ADMIN)
        created = self.client.post("/api/contests", json={
            "id": "hide-cup", "title": "Hide Cup",
            "starts_at": _iso(30), "ends_at": _iso(90),
            "problems": ["hanoi", "thai_time"], "hide_from_tasks": True},
            headers=browser)
        self.assertEqual(created.status_code, 200, created.text)

        self.login(STUDENT_A)
        listed = self.client.get("/api/contests").json()["contests"]
        mine = [c for c in listed if c["id"] == "hide-cup"][0]
        self.assertEqual(mine["status"], "upcoming")
        self.assertEqual(mine["problems"], [])
        detail = self.client.get("/api/contests/hide-cup").json()
        self.assertFalse(detail["problems_open"])
        self.assertEqual(detail["problem_details"], [])
        self.assertEqual(self.client.get("/api/problems/thai_time").status_code, 404)
        annc = self.client.get("/api/contests/hide-cup/announcements").json()
        self.assertTrue(annc.get("locked"))
        self.assertEqual(annc["announcements"], [])
        denied = self.client.post("/api/contests/hide-cup/announcements",
                                  json={"body": "hi"}, headers=browser)
        self.assertEqual(denied.status_code, 403, denied.text)

        self.login(ADMIN)
        post = self.client.post("/api/contests/hide-cup/announcements",
                                json={"body": "welcome"}, headers=browser)
        self.assertEqual(post.status_code, 200, post.text)
        seen = self.client.get("/api/contests/hide-cup/announcements").json()
        self.assertEqual([a["body"] for a in seen["announcements"]], ["welcome"])
        bad = self.client.post("/api/contests/hide-cup/announcements",
                               json={"body": ""}, headers=browser)
        self.assertEqual(bad.status_code, 400, bad.text)

        self.login(STUDENT_A)
        still = self.client.get("/api/contests/hide-cup/announcements").json()
        self.assertTrue(still.get("locked"))

        # start the round: list, details and announcements all open at once
        self.login(ADMIN)
        self.client.post("/api/contests", json={
            "id": "hide-cup", "title": "Hide Cup",
            "starts_at": _iso(-5), "ends_at": _iso(55),
            "problems": ["hanoi", "thai_time"], "hide_from_tasks": True},
            headers=browser)
        self.login(STUDENT_A)
        running = self.client.get("/api/contests/hide-cup").json()
        self.assertTrue(running["problems_open"])
        self.assertEqual([p["id"] for p in running["problem_details"]],
                         ["hanoi", "thai_time"])
        self.assertEqual([c["problems"] for c in
                          self.client.get("/api/contests").json()["contests"]
                          if c["id"] == "hide-cup"],
                         [["hanoi", "thai_time"]])
        open_annc = self.client.get("/api/contests/hide-cup/announcements").json()
        self.assertFalse(open_annc.get("locked", False))
        self.assertEqual([a["body"] for a in open_annc["announcements"]], ["welcome"])

        # mid-round add: a third problem joins live, readable immediately
        self.login(ADMIN)
        self.client.post("/api/contests", json={
            "id": "hide-cup", "title": "Hide Cup",
            "starts_at": _iso(-5), "ends_at": _iso(55),
            "problems": ["hanoi", "thai_time", "sum_even"], "hide_from_tasks": True},
            headers=browser)
        self.client.post("/api/contests/hide-cup/announcements",
                         json={"body": "q3 added"}, headers=browser)
        self.login(STUDENT_A)
        grown = self.client.get("/api/contests/hide-cup").json()
        self.assertEqual([p["id"] for p in grown["problem_details"]],
                         ["hanoi", "thai_time", "sum_even"])
        self.assertEqual(self.client.get("/api/problems/sum_even").status_code, 200)
        both = self.client.get("/api/contests/hide-cup/announcements").json()
        self.assertEqual([a["body"] for a in both["announcements"]],
                         ["welcome", "q3 added"])

        self.login(ADMIN)
        self.assertEqual(self.client.delete("/api/contests/hide-cup",
                                            headers=browser).status_code, 200)

    def test_contest_scores_do_not_touch_practice_stats(self):
        self.login(STUDENT_A)
        self.addCleanup(lambda: self.fake.tables.__setitem__("contests", []))
        # snapshot BEFORE seeding: stats cover every submission in the fake DB
        before_subs = (self.client.get("/api/stats").json()["stats"].get("thai_time")
                       or {}).get("subs", 0)
        self.fake.tables.setdefault("submissions", [])
        self.fake.tables["submissions"] += [
            {"id": "g1", "user_email": "global@example.com", "name": "Global",
             "problem": "thai_time", "score": 60, "accepted": False,
             "contest_id": None, "created_at": "2026-10-05T00:00:00+00:00"},
            {"id": "c1", "user_email": "global@example.com", "name": "Global",
             "problem": "thai_time", "score": 100, "accepted": True,
             "contest_id": "cup-x", "created_at": "2026-10-06T00:00:00+00:00"},
            {"id": "c2", "user_email": "contest-only@example.com", "name": "Contest Only",
             "problem": "hanoi", "score": 100, "accepted": True,
             "contest_id": "cup-x", "created_at": "2026-10-06T00:00:00+00:00"},
        ]
        try:
            server._cache.clear()      # practice stats are cached for 30s
            stats = self.client.get("/api/stats").json()["stats"]
            # the non-contest row counts, the contest row does not
            self.assertEqual(stats["thai_time"]["subs"], before_subs + 1)
            self.assertNotIn("hanoi", stats)
            hist = self.client.get("/api/history?limit=50").json()["submissions"]
            self.assertTrue(all("contest_id" in r for r in hist), "rows carry contest_id")
        finally:
            self.fake.tables["submissions"] = [
                r for r in self.fake.tables["submissions"] if r["id"] not in ("g1", "c1", "c2")]

    def test_health_and_problem_listing(self):
        h = self.client.get("/api/health").json()
        self.assertTrue(h["ok"])
        # Anonymous users see only publicly visible problems: hidden
        # contest-only problems are filtered from health (access control,
        # not a count bug).
        visible = [pid for pid in server.PROBLEMS
                   if pid not in server._hidden_pids()]
        self.assertEqual(sorted(h["problems"]), sorted(visible))
        self.login(STUDENT_A)
        detail = self.client.get("/api/problems/count_islands").json()
        self.assertEqual(detail["samples"], ["01", "02", "03"])
        # The public payload must not announce the hidden-test count.
        self.assertNotIn("hidden_tests", detail)


    def test_remember_me_restore_and_logout(self):
        # Long-lived login is opt-in; opting out leaves only the session.
        # Losing the session cookie must not log the user out while the
        # remember cookie survives; logout kills both.
        from urllib.parse import urlsplit as _urlsplit
        _parts = _urlsplit(server.FRONTEND_URL)
        browser = {"Origin": f"{_parts.scheme}://{_parts.hostname}"}
        self.login("remember.me@example.com")
        self.client.post("/api/auth/password", json={
            "email": "remember.me@example.com", "password": "testpass123", "remember": True},
            headers=browser)
        jar = self.client.cookies
        names = [c.name for c in jar.jar]
        self.assertIn("ng_remember", names)
        self.client.cookies.clear()
        self.login("remember.no@example.com", "No")
        self.assertNotIn("ng_remember", [c.name for c in self.client.cookies.jar])

        # back to the remembered account, then lose only the session cookie
        self.client.post("/api/auth/password", json={
            "email": "remember.me@example.com", "password": "testpass123", "remember": True},
            headers=browser)
        jar = self.client.cookies
        token = next(c.value for c in jar.jar if c.name == "ng_remember")
        jar.clear()
        solo = {"Cookie": f"ng_remember={token}"}
        self.assertIsNone(self.client.get("/api/auth/me", headers=solo).json()["user"])
        restored = self.client.get("/api/auth/restore", headers=solo).json()
        self.assertTrue(restored["restored"])
        me = self.client.get("/api/auth/me").json()
        self.assertEqual(me["user"]["email"], "remember.me@example.com")

        # garbage token restores nothing and never errors
        bad = {"Cookie": "ng_remember=bogus"}
        self.assertFalse(self.client.get("/api/auth/restore", headers=bad).json()["restored"])

        self.login("remember.me@example.com")
        out = self.client.post("/api/auth/logout", headers=browser)
        self.assertEqual(out.status_code, 200, out.text)
        left = [c.name for c in self.client.cookies.jar if c.value]
        self.assertNotIn("ng_session", left)
        self.assertNotIn("ng_remember", left)


class PreMigrationFallback(unittest.TestCase):
    """Production runs this code BEFORE supabase/schema.sql is re-run: the new
    submission columns/tables/views are missing. Identity migration is mandatory;
    this class models older judge storage, not an authentication fallback."""

    OLD_SUB_COLS = {"id", "created_at", "problem", "language", "filename", "name",
                    "user_email", "verdict", "accepted", "passed", "total", "score",
                    "counts", "results", "code", "detail"}

    @classmethod
    def setUpClass(cls):
        cls._ctx = TestClient(server.app)
        cls.client = cls._ctx.__enter__()
        cls.fake = FakeSupabase(
            columns={"submissions": cls.OLD_SUB_COLS,
                     "users": {"email", "name", "password_hash", "email_verified_at", "auth_version"},
                     "profiles": {"user_email", "display_name", "default_language"}},
            missing={"judge_jobs", "contests"}, views=False)
        server._sb = cls.fake

    @classmethod
    def tearDownClass(cls):
        server._sb = None
        cls._ctx.__exit__(None, None, None)

    def setUp(self):
        server._login_tries.clear()
        server._account_tries.clear()
        server._quota_hits.clear()
        server._cache.clear()
        server._contest_pid_cache["at"] = 0.0
        server._contest_pid_cache["all"] = set()
        server._contest_pid_cache["hide"] = set()
        server._reg_count_cache["at"] = 0.0
        # contests/registrations are per-test fixtures (submissions accumulate and
        # some tests assert on the running totals)
        self.fake.tables["contests"] = []
        self.fake.tables["contest_registrations"] = []

    def _register(self, email: str, name: str) -> None:
        self.client.cookies.clear()
        self.fake.table("users").upsert({"email": email, "name": name,
            "password_hash": server._hash_password("testpass123"),
            "email_verified_at": "2026-10-18T00:00:00Z", "auth_version": 1}, on_conflict="email").execute()
        r = self.client.post("/api/auth/password", json={"email": email, "password": "testpass123"})
        self.assertEqual(r.status_code, 200, r.text)

    def _submit_and_wait(self, problem: str, code: str) -> dict:
        from urllib.parse import urlsplit as _urlsplit
        _parts = _urlsplit(server.FRONTEND_URL)
        submit = self.client.post("/api/submit", data={
            "problem": problem, "language": "python", "code": code},
            headers={"Origin": f"{_parts.scheme}://{_parts.hostname}"})
        self.assertEqual(submit.status_code, 202, submit.text)
        jid = submit.json()["job_id"]
        deadline = time.time() + 60
        job = {}
        while time.time() < deadline:
            job = self.client.get(f"/api/jobs/{jid}").json()
            if job["status"] in ("done", "error"):
                return job
            time.sleep(0.05)
        self.fail(f"job never finished without the queue tables: {job}")

    def test_submit_still_judges_and_stores_on_old_schema(self):
        self._register("legacy@example.com", "Legacy")
        job = self._submit_and_wait("thai_time", THAI_REF)
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(job["result"]["score"], 100)

        rows = [r for r in self.fake.rows("submissions") if r.get("id") == job["result"]["id"]]
        self.assertEqual(len(rows), 1)
        # the traceability columns are dropped instead of losing the submission
        self.assertNotIn("tests_hash", rows[0])
        self.assertNotIn("grader_version", rows[0])

    def test_aggregates_fall_back_to_row_scan(self):
        self._register("legacy2@example.com", "Legacy2")
        self._submit_and_wait("thai_time", THAI_REF)

        stats = self.client.get("/api/stats")
        self.assertEqual(stats.status_code, 200, stats.text)
        self.assertIn("thai_time", stats.json()["stats"])
        self.assertGreaterEqual(stats.json()["stats"]["thai_time"]["accepted"], 1)

    def test_contests_report_unconfigured(self):
        body = self.client.get("/api/contests").json()
        self.assertFalse(body["configured"])
        self.assertEqual(body["contests"], [])
        self.assertIn("hint", body)

    def test_version_still_reports_hashes(self):
        body = self.client.get("/api/version").json()
        self.assertIn("thai_time", body["tests"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
