"""Retired global rankings; synthetic data only, no private problems required."""
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from critical_helpers import test_server

server = test_server()


class RemovedLeaderboard(unittest.TestCase):
    def test_old_endpoint_requires_login_without_database_access(self):
        database = Mock()
        with patch.object(server, "_sb", database):
            client = TestClient(server.app)
            try:
                for url in ("/api/leaderboard", "/api/leaderboard?limit=100"):
                    with self.subTest(url=url):
                        response = client.get(url)
                        self.assertEqual(response.status_code, 401)
                        self.assertEqual(response.json(), {"detail": "login required"})
            finally:
                client.close()
        database.table.assert_not_called()

    def test_global_board_is_absent_from_the_api_schema(self):
        self.assertNotIn("/api/leaderboard", server.app.openapi()["paths"])
