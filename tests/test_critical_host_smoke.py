"""The production smoke CLI must never read or send staging credentials."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('host_smoke', ROOT / 'deploy/host/smoke_https.py')
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


class SmokeCredentialBoundary(unittest.TestCase):
    def exercise(self, production, expected_count=89, reported_count=None):
        requests = []
        if reported_count is None:
            reported_count = expected_count
        key = 'synthetic-staging-test-key-not-a-secret-0000'

        def curl(command, **kwargs):
            self.assertEqual(command[0], 'curl')
            url = urlsplit(command[-1])
            self.assertEqual(url.scheme, 'https')
            self.assertEqual(url.hostname, 'newbiegrader.pages.dev' if production
                             else 'newbiegrader-host-staging.pages.dev')
            header_path = command[command.index('--header') + 1][1:]
            sent = dict(line.split(': ', 1) for line in Path(header_path).read_text().splitlines())
            requests.append(sent)
            if production:
                self.assertNotIn('x-newbiegrader-staging-key', sent)
            status, body, headers = 200, {}, []
            path = url.path
            if path == '/api/health':
                body = {'ok': True, 'count': reported_count}
            elif path == '/api/version':
                body = {'problems': reported_count, 'system': {'role': 'api', 'queue_mode': 'database'}}
            elif path == '/api/auth/me':
                body = {'user': None, 'is_admin': False}
                headers.append(('Cache-Control', 'no-store'))
                if not production and sent.get('x-newbiegrader-staging-key') != key:
                    status = 403
            elif path == '/api/home':
                body = {'problems_count': reported_count, 'problems': []}
            elif path.startswith('/api/problems') or path in ('/api/stats', '/api/contests', '/api/admin/overview'):
                status = 401
                headers.append(('Cache-Control', 'private, no-store'))
            elif path.startswith('/api/jobs/') or path in ('/api/submit', '/api/run'):
                status = 401
            elif path == '/api/auth/login':
                status = 307
                headers.extend([
                    ('Location', 'https://accounts.google.com/o/oauth2/v2/auth?redirect_uri='
                     'https%3A%2F%2Fnewbiegrader.pages.dev%2Fapi%2Fauth%2Fcallback'),
                    ('Set-Cookie', 'ng_session=synthetic; Path=/; Secure; HttpOnly; SameSite=none'),
                ])
            elif path == '/api/auth/callback':
                status = 307
                headers.append(('Location', 'https://newbiegrader.pages.dev/#/?login=failed'))
            elif path == '/api/auth/logout':
                if '--data-binary' in command:
                    data = Path(command[command.index('--data-binary') + 1][1:]).read_bytes()
                    self.assertGreater(len(data), 1024 * 1024)
                    status = 413
                elif sent.get('Origin') != 'https://newbiegrader.pages.dev':
                    status = 403
                else:
                    body = {'ok': True}
                    headers.extend((('Set-Cookie', name + '=; Max-Age=0; Path=/')
                                    for name in ('ng_session', 'ng_remember')))
            else:
                self.fail('Unexpected synthetic HTTP path')
            Path(command[command.index('--dump-header') + 1]).write_text(
                'HTTP/2 ' + str(status) + '\n' + ''.join(k + ': ' + v + '\n' for k, v in headers) + '\n')
            Path(command[command.index('--output') + 1]).write_text(json.dumps(body))
            return subprocess.CompletedProcess(command, 0, str(status), '')

        with tempfile.TemporaryDirectory() as directory:
            access = Path(directory) / 'absent-production-access.json'
            if not production:
                access.write_text(json.dumps({'STAGING_ACCESS_KEY': key}))
                access.chmod(0o600)
            argv = ['smoke_https.py', '--access-file', str(access), '--expected-samesite', 'none']
            if expected_count != 89:
                argv.extend(['--expected-problems', str(expected_count)])
            if production:
                argv.append('--production')
            output = io.StringIO()
            # The CLI intentionally sets a private umask for its process. A
            # test invokes main in-process, so restore it before later tests
            # create root-owned compiler fixtures for an unprivileged child.
            previous_umask = os.umask(0o077)
            try:
                with patch('sys.argv', argv), patch.object(smoke.subprocess, 'run', side_effect=curl), \
                        contextlib.redirect_stdout(output):
                    smoke.main()
            finally:
                os.umask(previous_umask)
            report = json.loads(output.getvalue())
        self.assertEqual(report['passed'], 21 if production else 23)
        self.assertFalse(report['real_login_verified'])
        self.assertFalse(report['authenticated_job_submitted'])
        self.assertTrue(requests)

    def test_production_never_requires_or_transmits_staging_secret(self):
        self.exercise(True)

    def test_staging_keeps_its_missing_and_wrong_key_checks(self):
        self.exercise(False)

    def test_production_catalog_count_can_be_reviewed_explicitly(self):
        self.exercise(True, expected_count=89)

    def test_missing_catalog_entries_fail_the_production_smoke(self):
        with self.assertRaisesRegex(RuntimeError, 'public GET health'):
            self.exercise(True, expected_count=89, reported_count=68)


if __name__ == '__main__':
    unittest.main()
