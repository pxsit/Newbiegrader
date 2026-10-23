# Revocable website sessions

This change requires the additive [SQL Editor patch](../supabase/session_revocation.sql)
before the new API is activated. The identical block is appended to
`supabase/schema.sql`. Existing account data, identities and job ownership are
preserved. Activation invalidates existing browser credentials once; users must
sign in again. Ordinary users and administrators use the same session boundary.

## Request and logout behavior

A successful password/email-code login creates a random 256-bit session token.
Only its SHA256 hash is stored in the server-only `public.web_sessions` table.
The signed HttpOnly session cookie persists the opaque token, without a `user`
claim. Identity is reconstructed by one indexed RPC joining the session with the
currently verified account and credential version. This replaces the previous
account lookup; it does not add a second database round trip on protected requests.
There is no positive authorization cache. Anonymous requests without a session
need no session lookup.

Guests can view the landing page and use authentication/recovery. Other website
pages redirect to login after the session/remember checks finish. The API denies
member endpoints with `401`, including catalog, statements, samples and PDFs.
`/api/home` supplies bounded landing previews; health/version/language diagnostics
remain public. Member responses use `private, no-store` and identity changes clear
the SPA caches. This boundary does not require a new database migration or key
rotation.

Session and remember cookies refer to the same revocable family. Normal logout
revokes every family represented by those signed browser cookies and clears the
cookies only after the database acknowledges success. It leaves another device's
separate session valid. A password/account-recovery version change invalidates
all previous families on their next request. Work already admitted to the durable
judge queue continues; authenticated polling/history access requires a valid login.

If the database/schema is unavailable, authenticated requests and logout return
503. The existing frontend preserves the workspace and reports failed logout;
it does not claim successful sign-out. Requests already authorized before logout
may finish, but a new authorization check after successful logout must fail.
An in-flight response may reissue an old cookie; its revoked DB record still
prevents authentication.

The existing rolling 30-day session-cookie age and opt-in 365-day remember age
are retained. Rows have longer storage retention so cleanup cannot prematurely
expire a valid cookie. Active retention renews only when fewer than 45 days remain,
rather than writing on every poll. Remember expiry is checked separately and does
not shorten an already-valid ordinary cookie. Login cleans at most 100 expired
rows using `SKIP LOCKED`; no extra scheduled job is created.

Google handoff tokens use a five-minute, initially pending family. An atomic
activation in the shared database consumes a handoff once across concurrent API
processes and restarts. Pending, expired, revoked or stale-account handoffs cannot
activate a session. All browser cookie flags, origin/CSRF controls and identity
proof requirements remain enforced.

## Database boundary

`web_sessions` has RLS and no anon/authenticated/public grants or policies.
Only the existing backend service role has the required table operations and
EXECUTE on the three `SECURITY INVOKER` RPCs. Each RPC has an empty fixed search
path and schema-qualified application relations. Do not add public policies to
silence the expected RLS-without-policy advisor notice.

The SQL Editor patch is transactional and re-runnable. It adds one table,
indexes and three functions; it does not rewrite existing users, passwords or
submissions. The service-role grants are explicit for Data API exposure. Refresh
the PostgREST schema cache and verify the exact function signatures before API
activation. Never deploy an identity-only fallback when the patch is missing.

## Activation and rollback

1. Qualify the reviewed source on Linux, including critical tests without skips,
   native isolation canaries, disposable PostgreSQL privilege/concurrency tests,
   and staging cookie replay checks. Compare protected-request latency with the
   previous one-query identity path.
2. Have the maintainer apply the reviewed SQL patch in Supabase SQL Editor, then
   verify table/RPC privileges and test the candidate against that exact project.
   The old API can continue running during this additive schema step.
3. Pause admission, drain actual queued/running jobs and live leases, preserve
   the newest UID ledger and take an encrypted configuration/data backup.
4. During activation, generate a fresh `SESSION_SECRET` on the host and store it
   only in the protected service configuration. Never expose it in arguments,
   logs, Git or operator output. This ends legacy cookies and pending login
   challenges once. Install the qualified release, verify services and reopen.
5. Test copied session/remember cookies over real HTTPS before and after logout,
   other-device continuity, failure handling, Python/C/C++ jobs and history.

**Rollback must retain the newly rotated signing secret**, even if reverting
application code/configuration. Do not restore the retired secret from a saved
`api.env`; doing so could revive pre-migration identity cookies in an old API.
New opaque cookies contain no identity for an old API to accept. A rollback to
legacy authentication also loses the new revocation capability and must be
reported as such; it is not a successful deployment of this feature.

## Backup and restore

Session rows are ephemeral credentials, excluded from scheduled data exports.
Durable account/history/problem data remain included. Restore into a fresh empty
session table; when replacing data in an existing database, the maintainer must
clear `public.web_sessions` before opening access. Never import an old session
export or replay session-creation calls from logs.

After database or configuration recovery, generate a fresh signing secret before
opening access. Every browser signs in again. Recovery must not resurrect an old
cookie by restoring its session row or signing key. The encrypted configuration
backup remains private; its credentials are not a reason to restore retired
browser signing keys unchanged.
