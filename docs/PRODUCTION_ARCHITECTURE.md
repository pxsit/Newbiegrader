# Current production architecture

Updated 2026-10-23 after server-side session revocation was deployed.
Verified application release: `8b2a484962e154a6806088823af4c005edafcba4`.
Documentation-only updates do not change that running application release.

- **Frontend:** Cloudflare Pages, `https://newbiegrader.pages.dev`, vanilla SPA.
- **Transport:** same-origin Pages Function → private `newbiegrader-pc-production`
  Worker → VPC service `newbiegrader-pc-api` → named `newbiegrader-pc` QUIC tunnel
  → loopback Nginx port 8081 → FastAPI port 8000. The origin has no public HTTP
  listener and no retired-host fallback.
- **Backend:** Ubuntu 26.04 x86_64 PC, Intel i5-10400T (6 physical cores /
  12 threads), about 7.1 GiB RAM and SSD storage; dedicated Python 3.13 venv.
  API and judging use separate systemd services. The API has one CPU / 320 MiB;
  the system Python is preserved and the project uses Python 3.13.17.
- **Judging:** exactly four `newbiegrader-worker@N.service` instances, one job
  per unit and serial tests within each job. Each has one CPU / 1 GiB, with an
  aggregate four-CPU / 4 GiB slice. Private workspace tmpfs, per-job cgroups,
  unique reserved UIDs, Landlock and seccomp remain mandatory. Per-problem limits
  and language memory allowances are unchanged; declared memory is capped at
  128 MiB and the overall submission deadline remains 240 seconds.
- **Admission:** four executing jobs plus up to 25 queued jobs; one active job
  per ordinary account. Queue size and measured job throughput do not establish
  a tested number of simultaneous users.
- **Startup/feedback latency:** precompiled trusted runtime, a credential-free
  executor that avoids web/database imports, immediate next-job claim and a
  one-second scheduled polling cap for active/head-of-queue jobs. Synthetic
  17-case jobs take 2.82–3.03 seconds; public Python/C/C++ checks observed
  3.75–4.81 seconds from submit to result. Larger jobs retain their actual
  runtime; see the [scoped measurements](releases/pc-execution-performance-2026-10-22.json).
- **Availability:** boot-enabled Tailscale, API, tunnel and all four workers;
  actual reboot recovery was verified. A dedicated sleep configuration disables
  suspend/hibernation. Tailnet device-key expiry is managed separately in its
  admin console and must be reviewed for unattended operation.
- **Database, identities and durable queue:** the existing Supabase project;
  no database migration or identity rewrite was needed to move hosts.
  Worker PostgREST connections use pooled HTTP/1.1 with certificate verification;
  the API retains the pinned SDK's existing transport. Ambiguous claim failures
  are not automatically replayed.
- **Website sessions:** opaque cookies identify a hash-only server-side family.
  One database RPC checks revocation and current account identity per protected
  request. Logout revokes that browser family; other devices remain active.
  Password/account recovery invalidates prior versions. See
  [session lifecycle and recovery](SESSION_REVOCATION.md).
- **Public abuse controls:** the private Worker limits API, authentication and
  execution requests by trusted client IP, with shared classroom IPs in mind.
  Turnstile protects email-code requests before their existing handler; password
  login, OTP verification and judging keep their established authorization.
  Exact settings and approximate local-counter limits are in the
  [transport guide](HOST_CLOUDFLARE_SETUP.md).
- **Files:** protected local public/private roots under
  `/var/lib/newbiegrader/problem-files/g1-4e9ba14273bc/`. Public files are
  root:newbiegrader-api with 0750 directories / 0440 files; private files are
  root:root with 0700 directories / 0400 files. No runtime cloud-file fallback.
- **Catalog:** 89 problems / 1,594 cases / 89 selected PDFs. Permanent slugs and
  submission history remain intact. The calibrated catalog number map is
  unchanged.
- **Backup:** the active PC runs the daily encrypted configuration/UID/table-row
  export to the private Supabase backup bucket. Ephemeral `web_sessions` rows are
  excluded; restore starts with no sessions and a fresh signing key. Protected
  file bytes have a
  separate verified encrypted off-host backup. The age private identity stays
  off-host. See the recovery guide for the exact coverage and limits.
- **Discord presence:** a separate unprivileged service checks the public HTTPS
  identity path, local API and all four workers every 30 seconds. It displays
  `Newbiegrader website` while online and goes invisible when those checks fail.
  Discord/network outages can delay or obscure status; it is an indicator rather
  than independent off-host monitoring. Alert routing stays in private configuration.
- **Watchdog:** the PC checks API, all four workers, tunnel, queue, disk, backup
  age, journal and the public HTTPS anonymous-identity endpoint every five minutes.
  The public check traverses Pages, the private bridge and tunnel without a session.
  Its existing bot token stays in root-only
  configuration; validation uses check-only mode without sending messages.

See [deployment](HOST_DEPLOYMENT.md), [private transport](HOST_CLOUDFLARE_SETUP.md),
[file storage](BACKEND_PROBLEM_FILES.md) and [backup/recovery](HOST_BACKUP_RESTORE.md).
Private release records retain measured test results and host configuration.
Application rollback uses a verified compatible release on this PC, retaining
current files, the newest append-only UID ledger and the newly rotated signing
key. Never restore the retired signing key from saved configuration.
