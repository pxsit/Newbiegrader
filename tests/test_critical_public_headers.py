"""The actual HTML entry point must have browser protections, not just JSON."""
from pathlib import Path
import unittest

from fastapi.testclient import TestClient
from critical_helpers import test_server


class PublicHeaders(unittest.TestCase):
    def test_pages_static_policy_preserves_only_the_required_resource_types(self):
        text = (Path(__file__).resolve().parents[1] / 'static/_headers').read_text()
        wildcard = text.split('\n/*\n', 1)[1].split('\n/', 1)[0]
        headers = dict(line.strip().split(': ', 1) for line in wildcard.splitlines()
                       if line.startswith('  '))
        directives = {}
        for entry in headers['Content-Security-Policy'].split(';'):
            key, *values = entry.strip().split()
            directives[key] = values
        self.assertEqual(directives['script-src'], ["'self'", 'https://challenges.cloudflare.com'])
        self.assertEqual(directives['connect-src'], ["'self'"])
        self.assertEqual(directives['base-uri'], ["'none'"])
        self.assertEqual(directives['object-src'], ["'none'"])
        self.assertEqual(directives['frame-ancestors'], ["'self'"])
        self.assertEqual(directives['frame-src'], ["'self'", 'https://challenges.cloudflare.com'])
        self.assertIn('https://fonts.googleapis.com', directives['style-src'])
        self.assertIn('https://fonts.gstatic.com', directives['font-src'])
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
        self.assertEqual(headers['X-Frame-Options'], 'SAMEORIGIN')
        self.assertEqual(headers['Strict-Transport-Security'], 'max-age=31536000')

    def test_api_headers_deny_base_and_object_injection(self):
        response = TestClient(test_server().app).get('/api/unknown-synthetic-path')
        self.assertEqual(response.status_code, 401)
        policy = response.headers['content-security-policy']
        for directive in ("base-uri 'none'", "object-src 'none'", "script-src 'self'",
                          "frame-ancestors 'self'"):
            self.assertIn(directive, policy)
        self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
