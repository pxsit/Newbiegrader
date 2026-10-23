"""HTTP-level golden-JSON contract tests for the DB-only API endpoints.

These pin today's response shapes (status + JSON body) of the endpoints that
only touch database tables, so the Supabase -> raw SQL rewrite can be checked
against them.  They need no problems/ directory and never run the judge: two
or three synthetic `grader.Problem` objects are injected into `server.PROBLEMS`.

    python -m unittest tests.test_api_contract -v
    UPDATE_GOLDEN=1 python -m unittest tests.test_api_contract -v   # (re)write goldens

Golden files live in tests/golden/<test name>.json; each holds one entry per
labelled response: {"status": int, "body": json}.  Bodies are normalised before
comparison: uuids -> "<uuid>", ISO timestamps -> "<ts>", volatile counters ->
"<int>".  Seeded ids start with 00000000-0000-4000-8000- and seeded timestamps
are in 2001 or 2099, and those are kept EXACT (they are deterministic and their
serialised format is part of the contract).  Everything else is compared exactly
(dict key order is irrelevant, list order is significant).

The storage backend is isolated behind `Seeder`: swap `Seeder.insert` (and the
`new_store()` factory) to seed Postgres instead of FakeSupabase.
"""
from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import patch
import difflib
import json
import os
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
GOLDEN_DIR = HERE / "golden"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

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

UPDATE = os.environ.get("UPDATE_GOLDEN", "") not in ("", "0")

STUDENT_A = "student.a@example.com"
STUDENT_B = "student.b@example.com"
CAROL = "carol@example.com"
ADMIN = "admin@example.com"
ADMIN2 = "admin2@example.com"      # database-backed admin (public.admins)
PASSWORD = "testpass123"


def _browser_origin() -> str:
    from urllib.parse import urlsplit
    parts = urlsplit(server.FRONTEND_URL)
    return f"{parts.scheme}://{parts.hostname}"


BROWSER = {"Origin": _browser_origin()}


# --------------------------------------------------------------------------
# seed data helpers
# --------------------------------------------------------------------------
def uid(n: int) -> str:
    return "00000000-0000-4000-8000-%012d" % n


def ts(day: int, hour: int = 0, minute: int = 0) -> str:
    return f"2001-01-{day:02d}T{hour:02d}:{minute:02d}:00+00:00"


FAR_START = "2099-01-01T00:00:00+00:00"
FAR_END = "2099-12-31T23:59:59+00:00"
FROZEN_AT = "2099-12-31T23:30:00+00:00"   # inside the c-run freeze window

SECRET = {"input": "SECRET-HIDDEN-INPUT", "expected": "SECRET-HIDDEN-EXPECTED",
          "actual": "SECRET-HIDDEN-ACTUAL", "diff": "SECRET-HIDDEN-DIFF",
          "stdout": "SECRET-HIDDEN-STDOUT", "stderr": "SECRET-HIDDEN-STDERR"}
LEAK_KEYS = ("input", "expected", "actual", "diff")


def _public_item(verdict: str = "AC") -> dict:
    return {"test": "01", "verdict": verdict, "wall_s": 0.01, "cpu_s": 0.01,
            "peak_mb": 9.0, "input": "1 2\n", "expected": "3\n",
            "actual": "3\n" if verdict == "AC" else "4\n"}


def _hidden_item(verdict: str = "AC") -> dict:
    # Deliberately stores leaked fields: the read path must strip them again.
    return {"test": "02", "verdict": verdict, "wall_s": 0.02, "cpu_s": 0.02,
            "peak_mb": 10.0, **SECRET}


class Seeder:
    """The only way tests put rows into the store (swap for Postgres later)."""

    def __init__(self, store):
        self.store = store

    def insert(self, table: str, row: dict) -> None:
        self.store.table(table).insert(dict(row)).execute()


def new_store():
    return FakeSupabase()


def submission(n: int, **kw) -> dict:
    row = {"id": uid(n), "created_at": ts(1, n), "user_email": STUDENT_A,
           "name": "Alice A", "problem": "alpha", "language": "python",
           "filename": None, "verdict": "AC", "accepted": True, "passed": 2,
           "total": 2, "score": 100, "counts": {"AC": 2},
           "results": [_public_item(), _hidden_item()], "code": "print(1)\n",
           "detail": "", "tests_hash": "h", "grader_version": "t",
           "kind": "submit", "contest_id": None, "job_id": None}
    row.update(kw)
    return row


def contest(cid: str, problems: list, start: str, end: str, **kw) -> dict:
    row = {"id": cid, "title": "Contest " + cid, "starts_at": start,
           "ends_at": end, "problems": problems, "freeze_minutes": 0,
           "show_samples": True, "hide_from_tasks": False,
           "created_by": ADMIN, "created_at": ts(1)}
    row.update(kw)
    return row


def _problem(pid: str, no: int, **kw) -> grader.Problem:
    base = dict(pid=pid, title=pid.title(), time_limit=1.0, memory_limit=256,
                checker="exact", tolerance=0.0, description="d",
                tests=[grader.TestCase("01", "1 2\n", "3\n"),
                       grader.TestCase("02", "SECRET-HIDDEN-INPUT", "SECRET-HIDDEN-EXPECTED")],
                difficulty="easy", level=1, tags=["t"], samples=["01"],
                hidden=False, no=no, is_published=True)
    base.update(kw)
    return grader.Problem(**base)


# --------------------------------------------------------------------------
# normalisation + golden comparison
# --------------------------------------------------------------------------
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_TS = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")
_VOLATILE_INT = {"starts_in_s", "ends_in_s", "uptime_s"}


def _norm_str(s: str) -> str:
    s = _UUID.sub(lambda m: m.group(0) if m.group(0).startswith("00000000-0000-4000-8000-")
                  else "<uuid>", s)
    return _TS.sub(lambda m: m.group(0) if m.group(0)[:3] == "200" or m.group(0)[:4] == "2099"
                   else "<ts>", s)


def normalize(value, key: str = ""):
    if key in _VOLATILE_INT and isinstance(value, int):
        return "<int>"
    if isinstance(value, dict):
        return {k: normalize(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    if isinstance(value, str):
        return _norm_str(value)
    return value


def dump(value) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)


def walk(value):
    """Yield every dict nested anywhere inside a JSON value."""
    if isinstance(value, dict):
        yield value
        for v in value.values():
            yield from walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from walk(v)


# --------------------------------------------------------------------------
class ApiContract(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls._ctx = TestClient(server.app)
        cls.client = cls._ctx.__enter__()
        cls._auth_stack = ExitStack()
        cls._auth_stack.enter_context(patch.dict(os.environ, {"SUPABASE_AUTH_KEY": "synthetic-public"}))
        cls._auth_stack.enter_context(patch.object(server, "_sb_url", "https://synthetic.supabase.co"))
        cls._auth_stack.enter_context(patch.object(server.email_auth, "request_code"))
        cls.password_hash = server._hash_password(PASSWORD)
        # Inject synthetic problems (no problems/ directory needed); restored below.
        cls._saved = (dict(server.PROBLEMS), dict(server.PROBLEMS_BY_NO),
                      dict(server._TESTS_HASH_CACHE))
        server.PROBLEMS.clear()
        server.PROBLEMS.update({
            "alpha": _problem("alpha", 1),
            "beta": _problem("beta", 2, hidden=True),
            "gamma": _problem("gamma", 3),
            "delta": _problem("delta", 4),
        })
        server._rebuild_problem_indexes()
        GOLDEN_DIR.mkdir(exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        server._sb = None
        server.PROBLEMS.clear()
        server.PROBLEMS.update(cls._saved[0])
        server.PROBLEMS_BY_NO.clear()
        server.PROBLEMS_BY_NO.update(cls._saved[1])
        server._TESTS_HASH_CACHE.clear()
        server._TESTS_HASH_CACHE.update(cls._saved[2])
        server._db_admin_emails = frozenset()
        cls._ctx.__exit__(None, None, None)
        cls._auth_stack.close()

    # ---------- per-test fixture ----------
    def setUp(self):
        self.client.cookies.clear()
        server._login_tries.clear()
        server._account_tries.clear()
        server._quota_hits.clear()
        server._cache.clear()
        server._contest_pid_cache.update({"at": 0.0, "all": set(), "hide": set(), "rows": []})
        server._reg_count_cache["at"] = 0.0
        server._reg_count_cache["counts"] = {}
        server._PROFILE_NAME_CACHE.clear()
        server._reg_table_ok["value"] = True
        server._annc_table_ok["value"] = True
        server._jobs.clear()
        for p in server.PROBLEMS.values():
            p.hidden = p.pid == "beta"
        self.store = new_store()
        server._sb = self.store
        self.seed = Seeder(self.store)
        self._seed_base()
        server._db_admin_emails = frozenset()
        server._load_db_admins_sync()
        self._cases: dict = {}
        self._golden: dict | None = None
        self._used: set = set()
        self.addCleanup(self._finish_golden)

    def _seed_base(self):
        s = self.seed
        users = [(ADMIN, "Admin Root", 0), (STUDENT_A, "Alice A", 1), (STUDENT_B, "Bob B", 2),
                 (ADMIN2, "Admin Two", 3), (CAROL, "Carol C", 4)]
        for email, name, day in users:
            s.insert("users", {"email": email, "name": name, "password_hash": self.password_hash,
                               "email_verified_at": ts(1), "auth_version": 1,
                               "created_at": ts(day + 1)})
        s.insert("admins", {"email": ADMIN2, "created_by": ADMIN, "created_at": ts(3)})

        s.insert("contests", contest("c-ended", ["alpha"], ts(1), ts(2)))
        s.insert("contests", contest("c-blind", ["delta"], ts(1, 1), FAR_END, show_samples=False))
        s.insert("contests", contest("c-run", ["gamma"], ts(1, 2), FAR_END, freeze_minutes=60))
        s.insert("contests", contest("c-up", ["beta"], FAR_START, "2099-01-02T00:00:00+00:00",
                                     hide_from_tasks=True))
        for cid, email, name, h in (("c-run", STUDENT_A, "Alice A", 1), ("c-run", STUDENT_B, "Bob B", 2),
                                    ("c-up", STUDENT_A, "Alice A", 3)):
            s.insert("contest_registrations", {"contest_id": cid, "user_email": email,
                                               "name": name, "registered_at": ts(1, h)})

        subs = [
            submission(1),
            submission(2, created_at=ts(2), user_email=STUDENT_B, name="Bob B", verdict="WA",
                       accepted=False, passed=1, score=50, counts={"AC": 1, "WA": 1},
                       results=[_public_item(), _hidden_item("WA")]),
            submission(3, created_at=ts(3), problem="beta", verdict="WA", accepted=False,
                       passed=0, score=0, counts={"WA": 2},
                       results=[_public_item("WA"), _hidden_item("WA")]),
            submission(4, created_at=ts(1, 12), contest_id="c-ended"),
            submission(5, created_at=ts(4), problem="gamma", contest_id="c-run", accepted=False,
                       score=50, verdict="WA", passed=1, counts={"AC": 1, "WA": 1}),
            submission(6, created_at=ts(5), problem="gamma", contest_id="c-run",
                       user_email=STUDENT_B, name=STUDENT_B, accepted=False, score=40,
                       verdict="WA", passed=1, counts={"AC": 1, "WA": 1}),
            submission(7, created_at=FROZEN_AT, problem="gamma", contest_id="c-run"),
            submission(8, created_at=ts(6), problem="delta", contest_id="c-blind"),
            submission(9, created_at=ts(7), user_email=STUDENT_B, name="Bob B", verdict="CE",
                       accepted=False, passed=0, score=0, counts={"CE": 1}, results=[],
                       detail="compile error text"),
        ]
        for row in subs:
            s.insert("submissions", row)

        s.insert("reports", {"id": uid(101), "created_at": ts(1), "problem": "alpha", "kind": "statement",
                             "message": "typo in statement", "status": "open",
                             "user_email": STUDENT_A, "name": "Alice A"})
        s.insert("reports", {"id": uid(102), "created_at": ts(2), "problem": "alpha", "kind": "tests",
                             "message": "bad test", "status": "open",
                             "user_email": STUDENT_B, "name": "Bob B"})
        s.insert("reports", {"id": uid(103), "created_at": ts(3), "problem": "beta", "kind": "other",
                             "message": "old report", "status": "closed",
                             "user_email": STUDENT_A, "name": "Alice A"})
        s.insert("contest_announcements", {"id": uid(201), "contest_id": "c-run", "body": "first note",
                                           "created_by": ADMIN, "created_at": ts(1, 3)})
        s.insert("contest_announcements", {"id": uid(202), "contest_id": "c-run", "body": "second note",
                                           "created_by": ADMIN, "created_at": ts(1, 4)})
        s.insert("contest_announcements", {"id": uid(203), "contest_id": "c-up", "body": "secret hint",
                                           "created_by": ADMIN, "created_at": ts(1, 5)})

    # ---------- http helpers ----------
    def login(self, email: str) -> None:
        self.client.cookies.clear()
        r = self.client.post("/api/auth/password", json={"email": email, "password": PASSWORD},
                             headers=BROWSER)
        self.assertEqual(r.status_code, 200, r.text)

    def anon(self) -> None:
        self.client.cookies.clear()

    def get(self, path, **params):
        return self.client.get(path, params=params or None, headers=BROWSER)

    def post(self, path, body=None):
        return self.client.post(path, json=body, headers=BROWSER)

    def put(self, path, body=None):
        return self.client.put(path, json=body, headers=BROWSER)

    def delete(self, path):
        return self.client.delete(path, headers=BROWSER)

    # ---------- golden machinery ----------
    @property
    def _golden_path(self) -> Path:
        return GOLDEN_DIR / (self._testMethodName[len("test_"):] + ".json")

    def _finish_golden(self):
        if UPDATE:
            self._golden_path.write_text(dump(self._cases) + "\n", encoding="utf-8")
            return
        if self._golden is not None:
            stale = set(self._golden) - self._used
            if stale:
                raise AssertionError(f"{self._golden_path.name}: golden labels never checked: {sorted(stale)}")

    def check(self, label: str, resp, status: int | None = None, mutate=None):
        """Normalise a response and compare it with its golden entry."""
        self.assertNotIn(label, self._cases, f"duplicate label {label}")
        if status is not None:
            self.assertEqual(resp.status_code, status, f"[{label}] {resp.text}")
        try:
            body = resp.json()
        except ValueError:
            body = {"__non_json__": resp.text}
        if resp.status_code >= 400:
            self.assertIsInstance(body, dict, f"[{label}] error body must be a JSON object")
            self.assertIn("detail", body, f"[{label}] error body needs 'detail'")
        body = normalize(body)
        if mutate:
            body = mutate(body)
        got = {"status": resp.status_code, "body": body}
        self._cases[label] = got
        self._used.add(label)
        if UPDATE:
            return resp
        if self._golden is None:
            if not self._golden_path.is_file():
                self.fail(f"golden file missing: {self._golden_path} (run with UPDATE_GOLDEN=1)")
            self._golden = json.loads(self._golden_path.read_text(encoding="utf-8"))
        if label not in self._golden:
            self.fail(f"[{label}] missing from {self._golden_path.name} (run with UPDATE_GOLDEN=1)")
        want, have = dump(self._golden[label]), dump(got)
        if want != have:
            diff = "\n".join(difflib.unified_diff(want.splitlines(), have.splitlines(),
                                                  "golden", "actual", lineterm="", n=3))
            self.fail(f"[{label}] response differs from {self._golden_path.name}:\n{diff}")
        return resp

    def assert_no_hidden_leak(self, resp, label: str = ""):
        """No input/expected/actual/diff on hidden items, nor any seeded secret text."""
        text = resp.text
        self.assertNotIn("SECRET-HIDDEN", text, f"[{label}] hidden test data leaked: {text[:300]}")
        for d in walk(resp.json()):
            if d.get("hidden") is True:
                for k in LEAK_KEYS + ("stdout", "stderr"):
                    self.assertNotIn(k, d, f"[{label}] hidden item exposes {k}")

    def test_anonymous_requests_are_rejected_everywhere(self):
        """MemberBoundaryMiddleware answers 401 before any handler runs."""
        self.anon()
        calls = [
            ("GET", "/api/history"), ("GET", f"/api/history/{uid(1)}"), ("GET", "/api/stats"),
            ("GET", "/api/reports"), ("POST", "/api/report"), ("PUT", f"/api/reports/{uid(101)}"),
            ("GET", "/api/admin/admins"), ("POST", "/api/admin/admins"),
            ("DELETE", f"/api/admin/admins/{ADMIN2}"), ("GET", "/api/admin/submissions"),
            ("GET", "/api/admin/overview"), ("GET", "/api/profile"), ("PUT", "/api/profile"),
            ("GET", "/api/contests"), ("POST", "/api/contests"), ("DELETE", "/api/contests/c-up"),
            ("GET", "/api/contests/c-up"), ("POST", "/api/contests/c-up/register"),
            ("DELETE", "/api/contests/c-up/register"),
            ("GET", "/api/contests/c-up/announcements"), ("POST", "/api/contests/c-up/announcements"),
        ]
        for method, path in calls:
            r = self.client.request(method, path, json={} if method in ("POST", "PUT") else None,
                                    headers=BROWSER)
            self.check(f"{method} {path}", r, 401)
            self.assertEqual(r.headers.get("cache-control"), "private, no-store")

    # ======================================================================
    # /api/history
    # ======================================================================
    def test_history_list_public_feed(self):
        # Every /api route except auth/health/version/home/languages needs a
        # session (MemberBoundaryMiddleware), so the "public feed" is what a
        # signed-in student with no submissions of their own sees.
        self.login(CAROL)
        r = self.check("default", self.get("/api/history"), 200)
        self.assert_no_hidden_leak(r, "default")
        ids = [s["id"] for s in r.json()["submissions"]]
        self.assertNotIn(uid(3), ids)      # hidden (contest-only) problem
        self.assertNotIn(uid(7), ids)      # inside the running contest's freeze window
        self.check("problem_slug", self.get("/api/history", problem="alpha"), 200)
        self.check("problem_number", self.get("/api/history", problem="1"), 200)
        self.check("problem_hidden_for_student", self.get("/api/history", problem="beta"), 200)
        self.check("unknown_problem", self.get("/api/history", problem="nope"), 200)
        self.check("mine_no_rows", self.get("/api/history", mine="true"), 200)
        self.anon()
        self.check("anonymous_mine", self.get("/api/history", mine="true"), 401)

    def test_history_list_limit_and_order(self):
        self.login(CAROL)
        self.check("limit_2", self.get("/api/history", limit=2), 200)
        self.check("limit_0_clamped_to_1", self.get("/api/history", limit=0), 200)
        self.check("limit_negative", self.get("/api/history", limit=-5), 200)
        self.check("limit_huge_clamped_to_100", self.get("/api/history", limit=100000), 200)
        self.check("limit_not_a_number", self.get("/api/history", limit="abc"), 422)

    def test_history_list_logged_in(self):
        self.login(STUDENT_A)
        self.check("student_all", self.get("/api/history"), 200)
        self.check("student_mine", self.get("/api/history", mine="true"), 200)
        self.check("student_mine_problem", self.get("/api/history", mine="true", problem="alpha"), 200)
        r = self.check("student_problem_peer_access", self.get("/api/history", problem="alpha"), 200)
        self.assertTrue(r.json()["peer_access"]["allowed"])
        self.check("student_mine_hidden_problem_visible_to_owner",
                   self.get("/api/history", mine="true", problem="beta"), 200)
        self.check("student_problem_beta_feed", self.get("/api/history", problem="beta"), 200)

    def test_history_list_admin(self):
        self.login(ADMIN)
        r = self.check("admin_all", self.get("/api/history", limit=100), 200)
        ids = [s["id"] for s in r.json()["submissions"]]
        self.assertIn(uid(3), ids)
        self.assertIn(uid(7), ids)
        self.check("admin_problem", self.get("/api/history", problem="gamma"), 200)

    def test_history_list_name_masking(self):
        # Submission 6 stored an email address as the display name.
        self.login(STUDENT_B)
        r = self.check("gamma_feed", self.get("/api/history", problem="gamma", limit=100), 200)
        names = {s["id"]: s["name"] for s in r.json()["submissions"]}
        self.assertEqual(names[uid(6)], "นักเรียน")

    def test_history_list_database_error(self):
        self.login(CAROL)
        self.store.missing.add("submissions")
        self.check("submissions_table_missing", self.get("/api/history"), 500)

    # ----------------------------------------------------------------------
    def test_history_detail(self):
        self.anon()
        self.check("anonymous", self.get(f"/api/history/{uid(1)}"), 401)
        self.login(STUDENT_A)
        r = self.check("owner_practice", self.get(f"/api/history/{uid(1)}"), 200)
        self.assert_no_hidden_leak(r, "owner_practice")
        self.assertNotIn("user_email", r.json())
        r = self.check("owner_ended_contest", self.get(f"/api/history/{uid(4)}"), 200)
        self.assert_no_hidden_leak(r, "owner_ended_contest")
        r = self.check("owner_running_contest", self.get(f"/api/history/{uid(5)}"), 200)
        self.assert_no_hidden_leak(r, "owner_running_contest")
        r = self.check("owner_blind_contest_no_samples", self.get(f"/api/history/{uid(8)}"), 200)
        self.assert_no_hidden_leak(r, "owner_blind")
        self.check("unknown_id", self.get(f"/api/history/{uid(9999)}"), 404)
        self.check("owner_hidden_problem_row", self.get(f"/api/history/{uid(3)}"), 200)

    def test_history_detail_peer_and_admin(self):
        self.login(STUDENT_B)
        r = self.check("peer_view_completed", self.get(f"/api/history/{uid(1)}"), 200)
        self.assert_no_hidden_leak(r, "peer")
        self.assertTrue(r.json()["peer_inspection"])
        self.check("peer_locked_hidden_problem", self.get(f"/api/history/{uid(3)}"), 403)
        self.check("peer_locked_running_contest", self.get(f"/api/history/{uid(5)}"), 403)
        self.check("peer_ce_row", self.get(f"/api/history/{uid(9)}"), 200)
        self.login(ADMIN)
        r = self.check("admin_views_student_row", self.get(f"/api/history/{uid(1)}"), 200)
        self.assert_no_hidden_leak(r, "admin_other")   # admin viewer, owner's policy applies
        r = self.check("admin_views_hidden_problem_row", self.get(f"/api/history/{uid(3)}"), 200)
        self.assert_no_hidden_leak(r, "admin_beta")

    # ======================================================================
    # /api/stats
    # ======================================================================
    def test_stats(self):
        self.anon()
        self.check("anonymous", self.get("/api/stats"), 401)
        self.login(CAROL)
        self.check("student", self.get("/api/stats"), 200)
        self.login(ADMIN)
        server._cache.clear()
        self.check("admin_same_public_view", self.get("/api/stats"), 200)

    # ======================================================================
    # /api/reports
    # ======================================================================
    def test_reports(self):
        self.anon()
        self.check("list_anonymous", self.get("/api/reports"), 401)
        self.check("create_anonymous", self.post("/api/report", {}), 401)
        self.login(STUDENT_A)
        self.check("list_mine", self.get("/api/reports"), 200)
        self.check("list_problem_filter", self.get("/api/reports", problem="beta"), 200)
        self.check("list_problem_filter_number", self.get("/api/reports", problem="1"), 200)
        self.check("list_limit_1", self.get("/api/reports", limit=1), 200)
        self.check("list_all_forbidden", self.get("/api/reports", all="true"), 403)
        self.check("create_ok", self.post("/api/report", {"problem": "alpha", "kind": "tests",
                                                          "message": "  something wrong  "}), 200)
        self.check("create_by_problem_number", self.post("/api/report", {"problem": "2", "kind": "other",
                                                                          "message": "x"}), 200)
        self.check("create_unknown_problem", self.post("/api/report", {"problem": "nope", "kind": "tests",
                                                                        "message": "x"}), 400)
        self.check("create_bad_kind", self.post("/api/report", {"problem": "alpha", "kind": "spam",
                                                                 "message": "x"}), 400)
        self.check("create_empty_message", self.post("/api/report", {"problem": "alpha", "kind": "tests",
                                                                      "message": "   "}), 400)
        self.check("create_long_message", self.post("/api/report", {"problem": "alpha", "kind": "tests",
                                                                     "message": "x" * 1001}), 400)
        self.check("create_not_object", self.post("/api/report", [1]), 400)
        self.check("list_mine_after_create", self.get("/api/reports"), 200)
        self.check("update_forbidden_for_student", self.put(f"/api/reports/{uid(101)}",
                                                           {"status": "closed"}), 403)

    def test_reports_admin(self):
        self.login(ADMIN)
        self.check("list_all", self.get("/api/reports", all="true"), 200)
        self.check("list_all_problem", self.get("/api/reports", all="true", problem="alpha"), 200)
        self.check("list_mine_as_admin", self.get("/api/reports"), 200)
        self.check("update_ok", self.put(f"/api/reports/{uid(101)}", {"status": "closed"}), 200)
        self.check("update_bad_status", self.put(f"/api/reports/{uid(101)}", {"status": "wontfix"}), 400)
        self.check("update_missing_status", self.put(f"/api/reports/{uid(101)}", {}), 400)
        # Current behaviour: an unknown report id is NOT a 404.
        self.check("update_unknown_id_still_ok", self.put(f"/api/reports/{uid(9999)}",
                                                          {"status": "open"}), 200)
        self.check("list_all_after_update", self.get("/api/reports", all="true"), 200)
        self.anon()
        self.check("update_anonymous", self.put(f"/api/reports/{uid(101)}", {"status": "open"}), 401)

    # ======================================================================
    # /api/admin/admins
    # ======================================================================
    def test_admin_admins(self):
        self.anon()
        self.check("list_anonymous", self.get("/api/admin/admins"), 401)
        self.login(STUDENT_A)
        self.check("list_student", self.get("/api/admin/admins"), 403)
        self.check("add_student", self.post("/api/admin/admins", {"email": CAROL}), 403)
        self.check("delete_student", self.delete(f"/api/admin/admins/{ADMIN2}"), 403)
        self.login(ADMIN)
        self.check("list_admin", self.get("/api/admin/admins"), 200)
        self.check("add_ok_normalised", self.post("/api/admin/admins", {"email": "  CAROL@Example.com "}), 200)
        self.check("list_after_add", self.get("/api/admin/admins"), 200)
        self.check("add_duplicate", self.post("/api/admin/admins", {"email": CAROL}), 409)
        self.check("add_env_admin", self.post("/api/admin/admins", {"email": ADMIN}), 409)
        self.check("add_invalid_email", self.post("/api/admin/admins", {"email": "not-an-email"}), 400)
        self.check("add_missing_email", self.post("/api/admin/admins", {}), 400)
        self.check("delete_env_admin", self.delete(f"/api/admin/admins/{ADMIN}"), 409)
        self.check("delete_invalid_email", self.delete("/api/admin/admins/nope"), 400)
        self.check("delete_not_found", self.delete("/api/admin/admins/ghost@example.com"), 404)
        self.check("delete_ok", self.delete(f"/api/admin/admins/{CAROL}"), 200)
        self.check("list_after_delete", self.get("/api/admin/admins"), 200)

    def test_admin_admins_as_database_admin(self):
        self.login(ADMIN2)
        self.check("list_as_db_admin", self.get("/api/admin/admins"), 200)
        self.check("delete_self", self.delete(f"/api/admin/admins/{ADMIN2}"), 400)
        self.check("admin_submissions_as_db_admin", self.get("/api/admin/submissions", per_page=20), 200)

    # ======================================================================
    # /api/admin/submissions
    # ======================================================================
    def test_admin_submissions(self):
        self.anon()
        self.check("anonymous", self.get("/api/admin/submissions"), 401)
        self.login(STUDENT_A)
        self.check("student", self.get("/api/admin/submissions"), 403)
        self.login(ADMIN)
        r = self.check("page1_default", self.get("/api/admin/submissions"), 200)
        self.assert_no_hidden_leak(r, "admin_submissions")
        for row in r.json()["submissions"]:
            self.assertNotIn("code", row)
            self.assertNotIn("results", row)
        self.check("problem_filter", self.get("/api/admin/submissions", problem="alpha"), 200)
        self.check("problem_filter_number", self.get("/api/admin/submissions", problem="3"), 200)
        self.check("problem_filter_none", self.get("/api/admin/submissions", problem="nope"), 200)
        self.check("per_page_invalid_falls_back_to_20", self.get("/api/admin/submissions", per_page=7), 200)
        self.check("page_zero_clamped", self.get("/api/admin/submissions", page=0), 200)
        self.check("page_not_a_number", self.get("/api/admin/submissions", page="x"), 422)

    def test_admin_submissions_pagination(self):
        # 30 extra rows; ties on created_at are broken by id DESC.
        for i in range(30):
            self.seed.insert("submissions", submission(
                300 + i, created_at=ts(10 + i // 10, i % 10), problem="alpha", results=[]))
        self.login(ADMIN)
        self.check("page1_per20", self.get("/api/admin/submissions", per_page=20, page=1), 200)
        self.check("page2_per20", self.get("/api/admin/submissions", per_page=20, page=2), 200)
        self.check("page1_per50", self.get("/api/admin/submissions", per_page=50), 200)
        self.check("filtered_page2", self.get("/api/admin/submissions", per_page=20, page=2,
                                              problem="alpha"), 200)
        # NOTE: a page past the end.  The fake returns an empty page with 200;
        # real PostgREST answers 416 for an out-of-range offset when rows exist,
        # which the current code would surface as 500 "database error".
        self.check("page_past_end", self.get("/api/admin/submissions", per_page=20, page=99), 200)

    def test_admin_submissions_tie_break(self):
        for i in (1, 2, 3):
            self.seed.insert("submissions", submission(400 + i, created_at=ts(20), results=[]))
        self.login(ADMIN)
        r = self.check("ties_id_desc", self.get("/api/admin/submissions", problem="alpha"), 200)
        ids = [s["id"] for s in r.json()["submissions"]][:3]
        self.assertEqual(ids, [uid(403), uid(402), uid(401)])

    # ======================================================================
    # /api/admin/overview
    # ======================================================================
    def test_admin_overview(self):
        self.anon()
        self.check("anonymous", self.get("/api/admin/overview"), 401)
        self.login(STUDENT_A)
        self.check("student", self.get("/api/admin/overview"), 403)
        self.login(ADMIN)

        def mask_host(body):
            if isinstance(body, dict) and isinstance(body.get("system"), dict):
                body["system"] = {"problems_loaded": body["system"].get("problems_loaded"),
                                  "__host_specific__": "masked"}
            return body

        self.check("admin", self.get("/api/admin/overview"), 200, mutate=mask_host)

    # ======================================================================
    # /api/profile
    # ======================================================================
    def test_profile(self):
        self.anon()
        self.check("get_anonymous", self.get("/api/profile"), 401)
        self.check("put_anonymous", self.put("/api/profile", {"display_name": "x"}), 401)
        self.login(STUDENT_A)
        self.check("get_default", self.get("/api/profile"), 200)
        self.check("put_set", self.put("/api/profile", {"display_name": "  Ally  ",
                                                       "default_language": "cpp"}), 200)
        self.check("get_after_put", self.get("/api/profile"), 200)
        self.check("put_update_existing", self.put("/api/profile", {"display_name": "Ally2"}), 200)
        self.check("get_after_update_clears_language", self.get("/api/profile"), 200)
        self.check("put_name_truncated_to_50", self.put("/api/profile", {"display_name": "n" * 80}), 200)
        self.check("put_bad_language", self.put("/api/profile", {"default_language": "cobol"}), 400)
        self.check("put_not_object", self.put("/api/profile", ["x"]), 400)
        self.check("put_numeric_name_coerced", self.put("/api/profile", {"display_name": 12345}), 200)
        self.login(STUDENT_B)
        self.check("other_user_unaffected", self.get("/api/profile"), 200)

    def test_profile_with_existing_row_and_nulls(self):
        self.seed.insert("profiles", {"user_email": STUDENT_B, "display_name": None,
                                      "default_language": None})
        self.login(STUDENT_B)
        self.check("null_columns_become_empty_strings", self.get("/api/profile"), 200)

    # ======================================================================
    # /api/contests
    # ======================================================================
    def test_contests_list(self):
        self.anon()
        self.check("anonymous", self.get("/api/contests"), 401)
        self.login(CAROL)
        self.check("student_without_registrations", self.get("/api/contests"), 200)
        self.login(STUDENT_A)
        self.check("student_a_registered_flags", self.get("/api/contests"), 200)
        self.login(STUDENT_B)
        self.check("student_b", self.get("/api/contests"), 200)
        self.login(ADMIN)
        self.check("admin_sees_upcoming_problems", self.get("/api/contests"), 200)

    def test_contests_list_when_empty_or_broken(self):
        self.login(CAROL)
        self.store.tables["contests"] = []
        self.check("empty", self.get("/api/contests"), 200)
        self.store.missing.add("contests")
        self.check("table_missing_degrades", self.get("/api/contests"), 200)

    def test_contest_detail(self):
        self.anon()
        self.check("anonymous", self.get("/api/contests/c-ended"), 401)
        self.login(CAROL)
        self.check("ended_student", self.get("/api/contests/c-ended"), 200)
        self.check("unknown", self.get("/api/contests/nope"), 404)
        self.check("upcoming_student_hides_problems", self.get("/api/contests/c-up"), 200)
        self.login(STUDENT_A)
        r = self.check("running_student_frozen_board", self.get("/api/contests/c-run"), 200)
        self.assertTrue(r.json()["frozen"])
        self.check("running_blind_student", self.get("/api/contests/c-blind"), 200)
        self.check("upcoming_student_registered", self.get("/api/contests/c-up"), 200)
        self.login(STUDENT_B)
        self.check("running_student_b", self.get("/api/contests/c-run"), 200)
        self.login(ADMIN)
        self.check("running_admin_live_board", self.get("/api/contests/c-run"), 200)
        self.check("upcoming_admin_participants", self.get("/api/contests/c-up"), 200)
        self.check("ended_admin", self.get("/api/contests/c-ended"), 200)

    def test_contests_upsert_and_delete(self):
        self.anon()
        self.check("upsert_anonymous", self.post("/api/contests", {}), 401)
        self.login(STUDENT_A)
        self.check("upsert_student", self.post("/api/contests", {}), 403)
        self.check("delete_student", self.delete("/api/contests/c-ended"), 403)
        self.login(ADMIN)
        good = {"id": "c-new", "title": "New Round", "starts_at": "2099-02-01T00:00:00Z",
                "ends_at": "2099-02-02T00:00:00Z", "problems": ["alpha", "2"],
                "freeze_minutes": 30, "show_samples": "false", "hide_from_tasks": "no"}
        self.check("create_ok", self.post("/api/contests", good), 200)
        self.check("list_after_create", self.get("/api/contests"), 200)
        self.check("update_existing", self.post("/api/contests", {**good, "title": "Renamed",
                                                                  "freeze_minutes": 99999,
                                                                  "problems": ["gamma"]}), 200)
        self.check("get_after_update", self.get("/api/contests/c-new"), 200)
        self.check("create_naive_timestamp_assumed_utc", self.post(
            "/api/contests", {**good, "id": "c-naive", "starts_at": "2099-03-01T09:00:00",
                              "ends_at": "2099-03-01T20:00:00+07:00", "title": ""}), 200)
        self.check("bad_id", self.post("/api/contests", {**good, "id": "X"}), 400)
        self.check("id_is_lowercased", self.post("/api/contests", {**good, "id": "C-UPPER"}), 200)
        self.check("bad_starts_at", self.post("/api/contests", {**good, "starts_at": "tomorrow"}), 400)
        self.check("ends_before_start", self.post("/api/contests", {**good, "ends_at": "2099-01-01T00:00:00Z"}), 400)
        self.check("unknown_problem", self.post("/api/contests", {**good, "problems": ["alpha", "nope"]}), 400)
        self.check("no_problems", self.post("/api/contests", {**good, "problems": []}), 400)
        self.check("bad_freeze", self.post("/api/contests", {**good, "freeze_minutes": "soon"}), 400)
        self.check("not_object", self.post("/api/contests", [1]), 400)
        self.check("delete_ok", self.delete("/api/contests/c-new"), 200)
        # Current behaviour: deleting an unknown contest is not a 404.
        self.check("delete_unknown_still_ok", self.delete("/api/contests/never-existed"), 200)
        self.check("get_deleted", self.get("/api/contests/c-new"), 404)

    # ----------------------------------------------------------------------
    def test_contest_registration(self):
        self.anon()
        self.check("register_anonymous", self.post("/api/contests/c-up/register"), 401)
        self.check("unregister_anonymous", self.delete("/api/contests/c-up/register"), 401)
        self.login(STUDENT_B)
        self.check("register_unknown_contest", self.post("/api/contests/nope/register"), 404)
        self.check("register_upcoming", self.post("/api/contests/c-up/register"), 200)
        self.check("register_again_idempotent", self.post("/api/contests/c-up/register"), 200)
        self.check("register_ended_refused", self.post("/api/contests/c-ended/register"), 400)
        self.check("detail_after_register", self.get("/api/contests/c-up"), 200)
        self.check("unregister_running_refused", self.delete("/api/contests/c-run/register"), 400)
        self.check("unregister_upcoming", self.delete("/api/contests/c-up/register"), 200)
        self.check("unregister_not_registered_still_ok", self.delete("/api/contests/c-up/register"), 200)
        self.check("unregister_unknown_contest", self.delete("/api/contests/nope/register"), 404)
        self.login(ADMIN)
        self.check("admin_register_ended_allowed", self.post("/api/contests/c-ended/register"), 200)
        self.check("admin_unregister_running_allowed", self.delete("/api/contests/c-run/register"), 200)
        self.check("list_as_admin_after", self.get("/api/contests"), 200)

    def test_contest_registration_uses_profile_display_name(self):
        self.seed.insert("profiles", {"user_email": CAROL, "display_name": "Caz", "default_language": "c"})
        self.login(CAROL)
        self.check("register", self.post("/api/contests/c-up/register"), 200)
        self.login(ADMIN)
        self.check("participants_show_profile_name", self.get("/api/contests/c-up"), 200)

    # ----------------------------------------------------------------------
    def test_contest_announcements(self):
        self.anon()
        self.check("anonymous", self.get("/api/contests/c-run/announcements"), 401)
        self.login(CAROL)
        self.check("running_student", self.get("/api/contests/c-run/announcements"), 200)
        self.check("upcoming_locked_for_student", self.get("/api/contests/c-up/announcements"), 200)
        self.check("ended_empty", self.get("/api/contests/c-ended/announcements"), 200)
        self.check("unknown_contest", self.get("/api/contests/nope/announcements"), 404)
        self.anon()
        self.check("post_anonymous", self.post("/api/contests/c-run/announcements", {"body": "x"}), 401)
        self.login(STUDENT_A)
        self.check("post_student", self.post("/api/contests/c-run/announcements", {"body": "x"}), 403)
        self.check("upcoming_locked_for_student_a", self.get("/api/contests/c-up/announcements"), 200)
        self.login(ADMIN)
        self.check("upcoming_visible_to_admin", self.get("/api/contests/c-up/announcements"), 200)
        self.check("post_ok", self.post("/api/contests/c-run/announcements", {"body": "  third note "}), 200)
        self.check("list_after_post", self.get("/api/contests/c-run/announcements"), 200)
        self.check("post_empty", self.post("/api/contests/c-run/announcements", {"body": " "}), 400)
        self.check("post_too_long", self.post("/api/contests/c-run/announcements", {"body": "x" * 2001}), 400)
        self.check("post_unknown_contest", self.post("/api/contests/nope/announcements", {"body": "x"}), 404)
        self.check("post_not_object", self.post("/api/contests/c-run/announcements", "str"), 400)


if __name__ == "__main__":
    unittest.main()
