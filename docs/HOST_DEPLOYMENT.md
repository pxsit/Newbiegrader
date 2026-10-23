# Native backend deployment

The backend runs on an Ubuntu PC with Python 3.13, Nginx and systemd. The
frontend remains on Cloudflare Pages; Supabase retains metadata, identities and
the durable queue. See [current architecture](PRODUCTION_ARCHITECTURE.md).
Deployment templates are in `deploy/host/`. Application source, protected
problem files and credentials are separate installation inputs.

## Host and resource allocation

The production PC has an i5-10400T (6 cores / 12 threads), about 7.1 GiB RAM and
SSD storage. Keep system Python unchanged. `bootstrap.sh` installs a separate
3.13 interpreter and venv under `/usr/local/lib/newbiegrader/` and preserves
existing firewall, Tailscale, disks and unrelated services.

To move an existing host to a newer Python, follow the
[Python upgrade guide](PYTHON_UPGRADE.md); re-running `bootstrap.sh` does not
replace an existing venv.

`newbiegrader-worker@1.service` through `@4.service` each run one judge job at a
time. Each unit has one CPU, 1 GiB memory, no swap and a private 128 MiB /
4096-inode workspace tmpfs. `newbiegrader-judge.slice` caps the entire fleet at
four CPUs / 4 GiB / 512 tasks. Tests within a submission remain serial, with the
existing language memory allowances, 128 MiB declared-memory cap and 240-second
overall timeout. The API has its own 320 MiB / one-CPU budget.

Each instance reads `/etc/newbiegrader/worker-N.env`, with:

- `GRADER_JUDGE_WORKERS=1` and `GRADER_CAPACITY_DIR=/var/lib/newbiegrader/capacity/worker-N`.
- `GRADER_JOB_CGROUP_ROOT` and `GRADER_CGROUP_ROOT` pointing to its own unit under
  `/sys/fs/cgroup/newbiegrader.slice/newbiegrader-judge.slice/`.
- The shared durable `GRADER_SANDBOX_IDENTITY_DIR=/var/lib/newbiegrader/sandbox-identities`.

Capacity locks are separate because each unit has a separately enforced
allocation. UID reservations remain shared and append-only. Never copy an old
ledger over a newer one; use `merge_uid_ledger.py` while all workers are stopped.
The API reads the aggregate slice through `GRADER_CGROUP_ROOT` and reports four
worker slots; this changes neither its own cgroup budget nor judging semantics.

## Staging and activation

1. Install a reviewed source snapshot in a new root-owned release directory.
   Exclude credentials, private problems, reference solutions and operator notes.
   Install the pinned `requirements.lock` into the project venv, then run
   `deploy/host/precompile_runtime.py --release /opt/newbiegrader/releases/<release>`
   using `/usr/local/lib/newbiegrader/venv/bin/python` from the reviewed release.
   Precompiling the trusted runtime avoids parsing the same Python modules for
   every sandbox launch; runtime services continue to disallow bytecode writes.
   Install public runtime packages with umask `0022`; an operator's private
   `0077` umask can make packages unreadable by the API account. Keep credentials
   and operator reports explicitly private. Before activation, verify dependency
   imports and CA loading as the actual unprivileged API user, for example:

   ```sh
   runuser -u newbiegrader-api -- /usr/local/lib/newbiegrader/venv/bin/python -c 'import fastapi, supabase, certifi, ssl; ssl.create_default_context(cafile=certifi.where())'
   ```

2. Transfer configuration privately. Preserve `SESSION_SECRET`, OAuth settings,
   Supabase credentials and cookie settings. Set `GRADER_ACCEPT_JOBS=0` in API and
   worker configuration. Keep `/etc/newbiegrader/worker-enabled` absent.
3. Restore protected file roots into empty directories and verify hashes, modes,
   the exported catalog, all samples and PDFs. See the [file guide](BACKEND_PROBLEM_FILES.md).
4. Install the unit templates and loopback-only Nginx configuration. Remove the
   default welcome site before unmasking Nginx. Create a separate named tunnel;
   preserve any unrelated `cloudflared.service` and its metrics listener.
5. Run native kernel canaries, `verify-recovery.sh`, the 21-test file/verdict
   qualification, `verify-workers.sh` and the Linux critical gate. A skip,
   unsupported control or missing cleanup is a failure. Qualify all four
   synthetic worker allocations concurrently before enabling real consumers.
6. Verify the gated HTTPS transport, session authorization, CSRF, body limits and
   anonymous access boundaries. Stop admission on the previous active host,
   drain queued/running jobs and live leases, stop every previous consumer, and
   merge the final UID reservations before enabling the new workers.
7. Change the Pages service binding to the verified private bridge. Create the
   worker gate, enable exactly instances 1–4, open admission and verify real
   Python/C/C++ jobs and linked history over production HTTPS.
8. Verify reboot recovery, encrypted off-host recovery and the backup/watchdog
   configuration. Schedule operations on the active host only.

All execution protections remain mandatory: per-job cgroups, private numeric
UIDs, capability drop, seccomp endpoint denial, Landlock filesystem rules,
resource limits and descendant cleanup. Do not weaken these to make a check pass.

Application rollback uses a verified release on this PC with the current
protected files and UID ledger. Pause/drain before changing a release. The
application has no fallback to a retired hosting provider.

## Watchdog configuration

Install `/etc/newbiegrader/host.env` as root:root 0600 with the complete fleet:

```ini
NEWBIEGRADER_WORKER_UNITS="newbiegrader-worker@1.service newbiegrader-worker@2.service newbiegrader-worker@3.service newbiegrader-worker@4.service"
NEWBIEGRADER_PUBLIC_URL=https://newbiegrader.pages.dev
```

The watchdog reads this through its unit's `EnvironmentFile`. Without this
setting its compatibility default checks the old singleton unit. Preserve the
root-only webhook/token and host label, run `--check-only` with the same host
environment, then enable the backup/alert/health timers on the active PC.
The optional HTTPS origin adds an anonymous `/api/auth/me` check through the
entire public transport. It must return an uncached 200 JSON object with
`user: null`; redirects, invalid TLS, oversized replies and unexpected identities
fail the check. Journal checks remain enabled with their original one-hour window.

## Worker database transport

`database_client.py` selects a pooled, TLS-verified HTTP/1.1 PostgREST client only
for the dedicated worker role. The pinned SDK's API/combined transport is
unchanged. Include this module in every release and container. Keep SDK versions
pinned and repeat transport tests before upgrading its private session factory.

This addresses observed HTTP/2 GOAWAY connection turnover during idle queue
polling. It is not a claim that all future network failures are prevented.
Do not replay a failed claim automatically: a disconnected POST can already
have leased a job. Preserve durable lease recovery and journal alerting.

## Automatic recovery and availability

Enable Tailscale, Nginx, API, the project tunnel and exactly worker instances 1–4
for boot. The production PC was rebooted and reconnected through Tailscale with
all four workers and the private tunnel active. Its unrelated tunnel is preserved.
For an always-on desktop host, install `server-sleep.conf.example` as
`/etc/systemd/sleep.conf.d/60-newbiegrader-server.conf`; this disables automatic
suspend/hibernation. Power must remain available; service startup does not turn
a physically powered-off PC on. Preserve existing Tailscale authentication and
verify device-key expiry policy separately from service enablement. The latest
2026-10-22 readback reports expiry on 2027-04-20. Review or disable device-key
expiry in the authorized tailnet administration for long-term unattended use;
local service enablement does not change that policy.

## Submission startup latency

Precompile each reviewed release and the dedicated runtime before activation.
The same synthetic 17-case workload took 7.6–8.3 seconds before preparation and
3.4–3.8 seconds afterward on this PC (two runs each for Python/C++). This is
startup evidence for short programs; expensive solutions and deep queues still
need their actual runtime. The subsequent execution-only refactor avoids
loading the HTTP/database stack for each isolated job and redundant Linux `ps`
samples after process exit. The same bounded workload now takes 2.82–3.03 seconds,
about 20% less than the already-precompiled baseline. Include `judge_execution.py`
in every application snapshot and container; it shares the existing grader and
feedback logic. See the [current measurement record](releases/pc-execution-performance-2026-10-22.json).

Workers immediately attempt the next durable claim after completing work, while
idle/outage backoff remains bounded. Browser polls for running jobs and the head
of the queue cap their scheduled wait at one second, with network time additional;
deeper queues retain traffic backoff. The
API has one CPU independently of the unchanged four-CPU judge slice. All test
cases, sandbox boundaries, language allowances and declared limits are preserved.

The public HTTPS verification used an existing verified operator session; it
did not repeat a fresh Google/OTP login. Both Python/C/C++ results and linked
history were checked. See [security validation](SECURITY_READINESS.md) for the
source fingerprint and the limits of these checks.

## Discord website presence

`newbiegrader-discord-presence.service` maintains the existing bot's Gateway
session with no subscribed intents. It reads the root-only alert configuration
through a systemd credential, under a dedicated `newbiegrader-presence` system
account with no home, login shell, sudo or capabilities,
64 MiB memory and a 5% CPU ceiling. The host lock already includes `websockets`;
no additional runtime dependency is required. Install the reviewed unit and
enable it for boot only on the active PC. Bootstrap creates the dedicated account;
existing installations must create that system account before starting the unit.
Run one presence process for this bot.

Every 30 seconds it requires all four workers, API and project tunnel to be active,
local catalog health to pass and public HTTPS `/api/auth/me` to return anonymous
identity. It displays Online after these checks and Invisible/Offline when they
fail. Startup and reconnect begin invisible until fresh checks pass. Graceful
shutdown closes the Gateway session normally; power/network loss appears offline
after Discord's session timeout, which may take a few minutes. A Discord outage
can make the bot offline while the website still works. Presence is an indicator,
not an independent external uptime monitor or proof that every submission works.

Heartbeat ACKs, resumable reconnects, backoff and a three-minute minimum between
new Identify sessions prevent reconnect storms. Permanent authentication/intent
errors stop the process rather than printing credentials or retrying rapidly.
The service sends no channel messages. The existing watchdog independently sends
ALERT/RECOVERED through REST to the operator-selected channel in the root-only
configuration. Changing the destination preserves the bot token and host label;
verify the exact guild/channel and posting permissions before replacing it.

See [Discord Gateway behavior](https://docs.discord.com/developers/events/gateway).

## Session schema and release activation

The current session release requires the maintainer-applied additive
[SQL Editor patch](../supabase/session_revocation.sql) before API activation.
Verify backend-only grants, staging revocation and concurrency, then pause
admission and drain queued/running jobs and leases. Rotate the host signing key
once during activation; users sign in again. Keep the new key on rollback.
Backups omit ephemeral session rows and recovery uses a fresh key. Follow the
[full session runbook](SESSION_REVOCATION.md) and
[measured qualification](releases/session-revocation-2026-10-23.json).
