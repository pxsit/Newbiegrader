---
status: proposed
---

# Run PostgreSQL 18 locally instead of Supabase

Production moved from the cloud to a single Ubuntu host, so the database moves with it: native PostgreSQL 18 on the host (systemd service, Unix socket with peer authentication, no DB credentials in env files), accessed with `psycopg` 3 and a pool. The 14 queue and auth plpgsql functions are kept and called directly; `database_client.py` and its HTTP/2 workaround are removed. Supabase roles and RLS are replaced by least-privilege Postgres roles (API, worker, and a NOLOGIN owner for the security definer functions), and `schema.sql` becomes a plain Postgres baseline. Problem tests, PDFs and backups move from Supabase Storage to local disk (`GRADER_FILE_STORAGE=local`).

## Considered Options

- **Postgres in Docker Compose**: rejected; workers are native, so a socket is simpler than a Docker network, and the data moves only once. Phase 4 containerizes the API only.
- **Keep hosted Supabase**: rejected; it adds a network hop and a cloud dependency to a system that is otherwise self-hosted.
- **Replace email-code login now**: deferred. Supabase Auth stays only for email one-time codes (`email_auth.py`, `SUPABASE_URL`/`SUPABASE_AUTH_KEY`). Google login and sessions never depended on it. Replacing it with own codes over SMTP is an optional later phase.

## Consequences

- The remaining Supabase project must not be paused for inactivity (paid tier or a keep-alive check), or email login stops working.
- Cutover is a maintenance window: `pg_dump` of the Supabase `public` schema, restore into Postgres 18, row-count checks and host canaries. Supabase stays read-only for about two weeks as rollback. `web_sessions` survives the restore.
- Backups: daily off-host `pg_dump`, last 7 days kept (RPO up to 24h accepted).
- Rollback to Supabase is clean only in the first hours; once the new database takes writes, going back loses them unless reverse-exported.
- Ambiguous RPC failures are still never auto-replayed, now for dropped connections mid-call.
- Migrations stay append-only and `schema.sql` must stay re-runnable; CI moves from Postgres 17 to 18.
