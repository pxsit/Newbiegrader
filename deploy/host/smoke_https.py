#!/usr/bin/env python3
"""Bounded anonymous/synthetic probes of host staging or approved production.

Never follows OAuth redirects, signs in, sends email or executes a job. Keys,
OAuth state and cookie values remain in private temporary files/memory.
"""
import argparse
from email.parser import Parser
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from urllib.parse import parse_qs, urlsplit
import uuid


BASE = 'https://newbiegrader-host-staging.pages.dev'
CANONICAL = 'https://newbiegrader.pages.dev'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--access-file', type=Path, default=Path.home() /
                        '.local/share/newbiegrader-private/cloudflare-staging/access-secrets.json')
    parser.add_argument('--expected-samesite', choices=('lax', 'strict', 'none'), default='lax',
                        help='Explicit expected value of the existing API session setting')
    parser.add_argument('--production', action='store_true',
                        help='Probe the fixed production URL without any staging credential')
    parser.add_argument('--expected-problems', type=int, default=89,
                        help='Reviewed expected catalog size (default: %(default)s)')
    args = parser.parse_args()
    if args.expected_problems < 1:
        parser.error('--expected-problems must be positive')
    base = CANONICAL if args.production else BASE
    access = None
    if not args.production:
        info = args.access_file.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise SystemExit('Access file must be a private owner-controlled regular file')
        access = json.loads(args.access_file.read_text())['STAGING_ACCESS_KEY']
        if not isinstance(access, str) or len(access) < 32 or '\n' in access or '\r' in access:
            raise SystemExit('Invalid staging access-key configuration')
    os.umask(0o077)
    passed = []

    def check(name, condition):
        if not condition:
            raise RuntimeError('Staging smoke check failed: ' + name)
        passed.append(name)

    with tempfile.TemporaryDirectory(prefix='newbiegrader-https-smoke-') as tmp:
        root = Path(tmp)
        cookie = root / 'cookies'
        cookie.touch(mode=0o600)

        def request(path, *, method='GET', key=access, headers=None, data=None):
            # Fixed HTTPS destination: a CLI flag cannot send the key elsewhere.
            if not path.startswith('/api/'):
                raise ValueError('Only staging API paths are allowed')
            h = dict(headers or {})
            if key is not None:
                h['x-newbiegrader-staging-key'] = key
            header_file = root / 'request-headers'
            header_file.write_text(''.join(k + ': ' + v + '\n' for k, v in h.items()))
            response_headers, response_body = root / 'response-headers', root / 'response-body'
            command = ['curl', '--silent', '--show-error', '--max-time', '30', '--proto', '=https',
                       '--header', '@' + str(header_file), '--cookie', str(cookie),
                       '--cookie-jar', str(cookie), '--dump-header', str(response_headers),
                       '--output', str(response_body), '--write-out', '%{http_code}',
                       '--request', method]
            if data is not None:
                body = root / 'request-body'
                body.write_bytes(data)
                command += ['--data-binary', '@' + str(body)]
            result = subprocess.run(command + [base + path], capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError('Staging HTTPS transport failed; no response/credential details printed')
            raw = response_headers.read_text()
            parsed = Parser().parsestr(raw.split('\n', 1)[1])
            return int(result.stdout), parsed, response_body.read_bytes()

        status, _, body = request('/api/health?smoke=' + uuid.uuid4().hex, key=None)
        health = json.loads(body)
        check('public GET health', status == 200 and health.get('ok') is True and health.get('count') == args.expected_problems)
        if not args.production:
            for label, key in [('missing', None), ('wrong', 'synthetic-invalid-access-key')]:
                status, _, _ = request('/api/auth/me', key=key)
                check(label + ' staging key denied', status == 403)
        status, _, body = request('/api/version?smoke=' + uuid.uuid4().hex)
        version = json.loads(body)
        check('host API/database role', status == 200 and version['system']['role'] == 'api'
              and version['system']['queue_mode'] == 'database' and version['problems'] == args.expected_problems)
        status, headers, body = request('/api/auth/me')
        me = json.loads(body)
        check('anonymous identity and auth cache', status == 200 and me.get('user') is None
              and me.get('is_admin') is False and headers.get('Cache-Control') == 'no-store')
        status, _, body = request('/api/home')
        preview = json.loads(body)
        check('bounded public landing', status == 200
              and preview.get('problems_count') == args.expected_problems
              and len(preview.get('problems', [])) <= 9)
        for path in ('/api/problems', '/api/problems/synthetic',
                     '/api/problems/synthetic/pdf', '/api/stats', '/api/contests'):
            status, headers, _ = request(path)
            check('anonymous member data denied ' + path,
                  status == 401 and 'no-store' in headers.get('Cache-Control', ''))
        status, _, _ = request('/api/admin/overview')
        check('anonymous admin denied', status == 401)
        status, _, _ = request('/api/jobs/synthetic-not-a-job')
        check('anonymous job lookup denied', status == 401)
        for path, form in [('/api/submit', b'problem=synthetic&language=python&code=print%281%29'),
                           ('/api/run', b'language=python&code=print%281%29')]:
            status, _, _ = request(path, method='POST', data=form, headers={
                'Content-Type': 'application/x-www-form-urlencoded', 'Origin': CANONICAL})
            check('anonymous execution denied ' + path, status == 401)
        status, _, _ = request('/api/auth/logout', method='POST', data=b'x' * (1024 * 1024 + 1),
                               headers={'Origin': CANONICAL})
        check('bounded request body', status == 413)

        # This only generates local OAuth state; no Google request is followed.
        status, headers, _ = request('/api/auth/login?next=%23%2Ftasks&remember=false')
        location = urlsplit(headers.get('Location', ''))
        callback = parse_qs(location.query).get('redirect_uri', [''])[0]
        cookies = headers.get_all('Set-Cookie', [])
        check('Google redirect remains manual/canonical', status == 307
              and location.hostname == 'accounts.google.com' and callback == CANONICAL + '/api/auth/callback')
        check('OAuth session cookie flags', any(c.startswith('ng_session=')
              and all(flag in c.lower() for flag in ('httponly', 'secure',
                      'samesite=' + args.expected_samesite, 'path=/'))
              and 'domain=' not in c.lower() for c in cookies))
        status, _, _ = request('/api/auth/logout', method='POST')
        check('cookie without Origin/Referer denied', status == 403)
        status, _, _ = request('/api/auth/logout', method='POST', headers={'Origin': 'https://evil.invalid'})
        check('untrusted Origin denied', status == 403)
        status, headers, _ = request('/api/auth/callback?code=synthetic-invalid&state=synthetic-invalid')
        check('wrong OAuth state denied before exchange', status == 307
              and headers.get('Location') == CANONICAL + '/#/?login=failed')
        status, headers, body = request('/api/auth/logout', method='POST', headers={'Origin': CANONICAL})
        cookies = headers.get_all('Set-Cookie', [])
        check('canonical logout', status == 200 and json.loads(body).get('ok') is True)
        check('multiple cookie deletions survive both proxies',
              all(any(c.startswith(name + '=') and ('max-age=0' in c.lower()
                      or '01 jan 1970' in c.lower()) for c in cookies)
                  for name in ('ng_session', 'ng_remember')))
    print(json.dumps({'base': base, 'vantage': 'operator host via curl, normal TLS validation',
                      'passed': len(passed), 'checks': passed, 'real_login_verified': False,
                      'session_samesite': args.expected_samesite,
                      'authenticated_job_submitted': False}, indent=2))


if __name__ == '__main__':
    main()
