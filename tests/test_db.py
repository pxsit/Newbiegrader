import threading
import unittest

import psycopg

import db
from tests.pg_harness import ScratchDatabase

SCRATCH = None


def setUpModule():
    global SCRATCH
    SCRATCH = ScratchDatabase()


def tearDownModule():
    if SCRATCH is not None:
        SCRATCH.close()


class DatabaseCase(unittest.TestCase):
    role = "newbiegrader_api"

    def setUp(self):
        SCRATCH.reset()
        self.db = db.Database(SCRATCH.dsn, min_size=1, max_size=4, checkout_timeout=2,
                              statement_timeout_ms=1500, set_role=self.role)
        self.db.open()
        self.addCleanup(self.db.close)


class RowShapeTests(DatabaseCase):
    def test_values_are_json_compatible(self):
        self.db.execute("insert into submissions(id, problem, language, verdict, counts, created_at) "
                        "values (%s, 'p', 'c', 'AC', %s, %s)",
                        ("11111111-1111-1111-1111-111111111111", psycopg.types.json.Jsonb({"AC": 2}),
                         "2026-10-24 12:00:00.5+00"))
        row = self.db.fetch_one("select * from submissions")
        self.assertEqual(row["id"], "11111111-1111-1111-1111-111111111111")
        self.assertEqual(row["created_at"], "2026-10-24T12:00:00.5+00:00")
        self.assertEqual(row["counts"], {"AC": 2})
        self.assertIs(row["accepted"], False)

    def test_timestamp_without_fraction_and_numeric(self):
        self.assertEqual(self.db.fetch_one("select '2026-10-24 12:00:00+07'::timestamptz as t")["t"],
                         "2026-10-24T05:00:00+00:00")
        row = self.db.fetch_one("select 3::numeric as a, 2.5::numeric as b, 9::bigint as c")
        self.assertEqual((row["a"], row["b"], row["c"]), (3, 2.5, 9))
        self.assertIsInstance(row["a"], int)

    def test_fetch_all_empty(self):
        self.assertEqual(self.db.fetch_all("select * from submissions"), [])
        self.assertIsNone(self.db.fetch_one("select * from submissions"))


class StatementTests(DatabaseCase):
    def test_parameters_are_not_interpolated(self):
        hostile = "x'; drop table submissions; --"
        self.db.execute("insert into profiles(user_email) values (%s)", (hostile,))
        self.assertEqual(self.db.fetch_one("select user_email from profiles")["user_email"], hostile)
        self.assertEqual(self.db.fetch_one("select count(*) as n from submissions")["n"], 0)

    def test_modify_returns_rows_and_execute_returns_count(self):
        rows = self.db.modify("insert into admins(email) values (%s) returning email", ("a@x.io",))
        self.assertEqual(rows, [{"email": "a@x.io"}])
        self.assertEqual(self.db.execute("delete from admins where email = %s", ("a@x.io",)), 1)

    def test_transaction_commits_and_rolls_back(self):
        with self.db.transaction() as tx:
            tx.execute("insert into admins(email) values ('a@x.io')")
        with self.assertRaises(RuntimeError):
            with self.db.transaction() as tx:
                tx.execute("insert into admins(email) values ('b@x.io')")
                raise RuntimeError("abort")
        self.assertEqual([r["email"] for r in self.db.fetch_all("select email from admins")], ["a@x.io"])

    def test_statement_timeout_is_enforced(self):
        with self.assertRaises(psycopg.errors.QueryCanceled):
            self.db.fetch_one("select pg_sleep(5)")
        self.assertEqual(self.db.fetch_one("select 1 as n")["n"], 1)  # connection still usable


class CallTests(DatabaseCase):
    def test_named_call_with_jsonb_and_arrays(self):
        out = self.db.call("judge_queue_admit", p_job={"problem": "p", "language": "c", "user_email": "u@x.io"},
                           p_queue_max=10, p_track_active=True, p_quota_limit=0, p_quota_window=0)
        self.assertEqual(out["job"]["problem"], "p")
        self.assertEqual(self.db.call("judge_queue_diagnostics", read_only=True), {"queued": 1, "running": 0})
        res = self.db.call("auth_session_revoke", p_token_hashes=["a" * 64])
        self.assertEqual(res, {"ok": True})

    def test_function_name_is_validated(self):
        with self.assertRaises(ValueError):
            self.db.call("x(); drop table users; --")
        with self.assertRaises(ValueError):
            self.db.call("judge_queue_diagnostics", **{"a); drop": 1})


class WorkerRoleTests(DatabaseCase):
    role = "newbiegrader_worker"

    def test_worker_cannot_read_accounts_but_can_claim(self):
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            self.db.fetch_all("select * from users")
        self.assertEqual(self.db.call("judge_queue_diagnostics"), {"queued": 0, "running": 0})
        self.assertIsNone(self.db.call("judge_queue_claim", p_worker="w", p_lease_seconds=60))


class FailureTests(DatabaseCase):
    def _kill_backends(self):
        with SCRATCH.connect() as conn:
            conn.execute("select pg_terminate_backend(pid) from pg_stat_activity "
                         "where datname = current_database() and application_name = 'newbiegrader' "
                         "and pid <> pg_backend_pid()")

    def test_read_is_retried_after_connection_loss(self):
        self.assertEqual(self.db.fetch_one("select 1 as n")["n"], 1)
        self._kill_backends()
        self.assertEqual(self.db.fetch_one("select 2 as n")["n"], 2)

    def test_write_on_dead_connection_is_ambiguous_not_replayed(self):
        self.db.execute("insert into admins(email) values ('seed@x.io')")
        # The INSERT is in flight when its backend is terminated, so the outcome is
        # unknown and must not be retried.
        killer = threading.Timer(0.7, self._kill_backends)
        killer.start()
        self.addCleanup(killer.join)
        with self.assertRaises(db.AmbiguousResult):
            self.db.modify("insert into admins(email) select 'late@x.io' "
                           "from (select pg_sleep(1.4)) s returning email")
        emails = {r["email"] for r in self.db.fetch_all("select email from admins")}
        self.assertNotIn("late@x.io", emails)

    def test_pool_exhaustion_reports_unavailable(self):
        held = []
        gate = threading.Event()

        def hold():
            with self.db.transaction() as tx:
                tx.fetch_one("select 1")
                held.append(1)
                gate.wait(5)
        threads = [threading.Thread(target=hold) for _ in range(4)]
        for t in threads:
            t.start()
        while len(held) < 4:
            pass
        try:
            with self.assertRaises(db.DatabaseUnavailable):
                self.db.fetch_one("select 1")
        finally:
            gate.set()
            for t in threads:
                t.join()


if __name__ == "__main__":
    unittest.main()
