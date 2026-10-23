# Moving the database from Supabase to native PostgreSQL 18

Status: draft. It describes the cutover for [ADR 0002](adr/0002-local-postgres-replaces-supabase.md).
PR A (schema, `db.py`, tests) is in the repository. The application still talks
to Supabase until PR B lands, so run this guide only with the PR B and PR C
release. Steps marked **(PR C)** name scripts and units that do not exist yet.

Supabase Auth stays in place only to send and verify email one-time codes
(`email_auth.py`). Everything else moves: tables, queue and auth functions,
sessions, and backups.

## What moves and what does not

| Item | Before | After |
|---|---|---|
| Tables, views, 14 SQL functions | Supabase Postgres | native PostgreSQL 18, `db/schema.sql` |
| API and worker access | PostgREST with the service key | Unix socket, peer auth, roles `newbiegrader_api` / `newbiegrader_worker` |
| Problem files and PDFs | backend disk | unchanged |
| Email one-time codes | Supabase Auth | unchanged (`SUPABASE_URL`, `SUPABASE_AUTH_KEY` only) |
| Backups | table export to a Supabase bucket | daily `pg_dump` copied off-host, 7 days kept **(PR C)** |
| `web_sessions` | Supabase | must survive the move, see step 7 |

No database password exists after the move. The connection is the Unix socket
`/var/run/postgresql`, and the kernel-reported OS user selects the role:
`newbiegrader-api` becomes `newbiegrader_api`, and the worker units (run as
root) become `newbiegrader_worker`. Sandboxed judge jobs run as private UIDs
with no mapping and cannot open sockets at all.

## Before the maintenance window

1. **Confirm the Ubuntu release.** `lsb_release -rs`. 26.04 ships PostgreSQL 18
   in its own archive. 24.04 ships 16, so add PGDG (`noble-pgdg`).
2. **Install PostgreSQL 18 and the 18 client.** Use `postgresql-18` and
   `postgresql-client-18` so `pg_dump` is at least as new as the server.
   **(PR C)** `deploy/host/` will carry a script that also puts `postgresql.service`
   in its own systemd slice (`MemoryMax` about 1 GiB, small `shared_buffers`) so a
   judge storm cannot OOM it on the 7 GiB host.
3. **Install the auth files.** Copy `deploy/host/pg_hba.conf.example` and
   `pg_ident.conf.example` to the cluster config directory (without `.example`),
   then `systemctl reload postgresql`. Check them first with
   `sh tools/check_pg_auth_config.sh` as the `postgres` OS user.
4. **Create the database and apply the baseline** (as the `postgres` OS user):

   ```
   createdb newbiegrader
   psql -v ON_ERROR_STOP=1 -d newbiegrader -f db/schema.sql
   psql -v ON_ERROR_STOP=1 -d newbiegrader -f db/schema.sql   # must be a no-op
   psql -v ON_ERROR_STOP=1 -d newbiegrader -f tests/role_privileges_sql.sql
   ```

   The privilege test fails if the worker can read `users`, if any function is
   security definer, or if anything is granted to PUBLIC.
5. **Size connections.** Default `max_connections=100` is enough: the API pool is
   16 (`GRADER_DB_POOL_MAX`) and each worker uses 2. Keep `statement_timeout` and
   `idle_in_transaction_session_timeout` as the application sets them.
6. **Compare columns.** The baseline folds every Supabase migration into the
   `create table` statements, so a table that drifted in production would lose
   data on restore. Run `docs/release/schema_preflight.sql` against Supabase,
   then list columns on both sides and diff them:

   ```
   select table_name, column_name, data_type, is_nullable
   from information_schema.columns where table_schema = 'public'
   order by table_name, ordinal_position;
   ```

   Any column present in Supabase but absent from `db/schema.sql` must be added to
   the baseline before cutover, or consciously dropped.
7. **Rehearse on a copy.** Do steps A to E below against a scratch database from a
   fresh dump and run the host canaries. Do not skip this: restore fidelity is
   the main risk.

## Cutover (maintenance window)

A. **Stop admission and drain.** Set `GRADER_ACCEPT_JOBS=0`, wait for queued and
   running jobs and live leases to reach zero, then stop the API and
   `newbiegrader-worker@*` units. Keep the session secret (`SESSION_SECRET`) and
   the sandbox UID ledger.

B. **Dump Supabase.** Use the 18 client, data only, `public` schema:

   ```
   pg_dump "$SUPABASE_DB_URL" --schema=public --data-only --no-owner \
     --format=custom --file=/root/newbiegrader-supabase.dump
   ```

   `SUPABASE_DB_URL` is the direct (non-pooled) connection string from the
   Supabase dashboard. Keep the dump private (`0600`, outside Git); it holds
   password hashes and submitted code.

C. **Restore into the baseline.** The schema already exists, so load data only.
   Triggers and foreign keys (`web_sessions` references `users`) need a
   superuser:

   ```
   sudo -u postgres pg_restore --data-only --disable-triggers \
     --exit-on-error -d newbiegrader /root/newbiegrader-supabase.dump
   ```

D. **Verify.** Compare row counts table by table against Supabase and expect
   them to match exactly:

   ```
   select 'submissions', count(*) from submissions union all
   select 'users', count(*) from users union all
   select 'judge_jobs', count(*) from judge_jobs union all
   select 'contests', count(*) from contests union all
   select 'problems', count(*) from problems;   -- and the remaining tables
   ```

   Then check that views return rows (`leaderboard_scores`, `problem_stats`), that
   no uuid or timestamp column came back null, and that `web_sessions` kept its
   rows (it is the one table whose loss logs every user out; the dump above
   includes it, so users stay signed in).

E. **Reset sequences.** The tables use uuid keys, so there are no sequences to
   reset. If a later migration adds a serial or identity column, reset it here
   with `setval`.

F. **Switch the services.** In `/etc/newbiegrader/api.env` and `worker.env`
   remove `SUPABASE_KEY` and add nothing else for the database: the default DSN
   is the local socket. Keep `SUPABASE_URL` and `SUPABASE_AUTH_KEY` for email
   codes. Start the API, then the workers, then run the host canaries
   (`tools/host_*canary*.py`) and a smoke login. Re-enable admission last.

G. **Enable backups and a keep-alive (PR C).** Turn on the daily `pg_dump` timer,
   confirm the first dump restores into a scratch database, and add the
   Supabase Auth keep-alive check: the free tier pauses on inactivity, and a
   paused project stops sending login codes.

## Rollback

- **Within the first hours, before the new database takes writes you want to
  keep:** stop the services, restore the previous env files (with `SUPABASE_KEY`)
  and start them again on the old release. Supabase has not been touched, so
  this is clean.
- **After the new database has taken writes:** going back loses every submission,
  account change and queue event since cutover unless you first export those rows
  back (there is no automatic reverse sync). Decide a deadline for "no return"
  before the window opens.
- Keep Supabase read-only for about two weeks as a data reference, then delete
  the project data.

## After cutover

- Recovery drill: restore the newest dump into a scratch database and run the
  privilege test and a read-only API check against it.
- Remove `database_client.py`, the `supabase` dependency and any
  `SUPABASE_KEY` references (done in PR B and PR C).
- Update `docs/PRODUCTION_ARCHITECTURE.md`, `docs/HOST_DEPLOYMENT.md`,
  `.env.example` and `CLAUDE.md` to describe PostgreSQL (PR C).

## Known behaviour differences to expect

These come from the contract tests (`tests/test_api_contract.py`) and are
decided deliberately in PR B:

- Writes to an unknown id keep returning `200 {"ok": true}` (report update,
  contest delete, unregister).
- A malformed uuid in `/api/history/{sid}` returns 404, not a database 500.
- A page past the end of `/api/admin/submissions` returns 200 with an empty list.
- Timestamps keep the `YYYY-MM-DDTHH:MM:SS(.fff)+00:00` form.
