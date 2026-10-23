import re
import unittest
from pathlib import Path

HOST = Path(__file__).resolve().parent.parent / "deploy" / "host"


def rules(name):
    lines = (HOST / name).read_text(encoding="utf-8").splitlines()
    return [line.split() for line in lines if line.strip() and not line.lstrip().startswith("#")]


class PostgresAuthConfig(unittest.TestCase):
    def test_hba_is_peer_only_and_ends_closed(self):
        hba = rules("pg_hba.conf.example")
        methods = {r[0]: r[3] if r[0] == "local" else r[4] for r in hba}
        self.assertEqual(methods["host"], "reject")
        for rule in hba:
            method = rule[3] if rule[0] == "local" else rule[4]
            self.assertIn(method, ("peer", "reject"))
        self.assertEqual(sum(r[0] == "local" and r[3] == "peer" for r in hba), 3)
        self.assertEqual(hba[-2][:4], ["local", "all", "all", "reject"])
        self.assertEqual(hba[-1][0], "host")

    def test_only_the_service_users_are_mapped(self):
        ident = {tuple(r[1:]) for r in rules("pg_ident.conf.example")}
        self.assertEqual(ident, {("newbiegrader-api", "newbiegrader_api"),
                                 ("root", "newbiegrader_worker")})

    def test_mapped_users_match_the_systemd_units(self):
        def unit_user(name):
            text = (HOST / name).read_text(encoding="utf-8")
            return re.search(r"^User=(\S+)", text, re.M).group(1)
        self.assertEqual(unit_user("newbiegrader-api.service"), "newbiegrader-api")
        self.assertEqual(unit_user("newbiegrader-worker@.service"), "root")


if __name__ == "__main__":
    unittest.main()
