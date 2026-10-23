# Load handling and rollout

Current production is the four-worker Ubuntu PC described in
[the architecture](PRODUCTION_ARCHITECTURE.md) and [host runbook](HOST_DEPLOYMENT.md).
The frontend stays static and FastAPI remains the API. Judge work now runs in supervised subprocesses; compilation, submissions, playground runs, and rejudges share host execution slots. Tests within a submission run serially. Output streams are bounded during collection, and timeout/cancellation waits for process cleanup before returning a slot.

## Queue modes and roles

`GRADER_QUEUE_MODE=local` with `GRADER_ROLE=combined` is the compatible default. Run one Uvicorn process in this mode. The process owns admission, the local queue, and workers; Supabase job rows support recovery when configured. Missing legacy schema can still degrade to memory in local mode. This mode is not a coordinated multi-instance queue.

`GRADER_QUEUE_MODE=database` requires the appended queue migration in `supabase/schema.sql`. Admission, per-account quotas, job claims, heartbeats, completion fencing, and history persistence use service-role-only RPCs. A database failure rejects new work rather than silently accepting a local-only job. Status reads use shared database state. Students take priority over queued bulk rejudges; rejudge admission cannot exceed the queue's remaining space.

Roles in database mode:

| Role | Command | Responsibility |
|---|---|---|
| `combined` | `python server.py` or Uvicorn | HTTP API and judge consumers |
| `api` | `uvicorn server:app --host 127.0.0.1 --port 8000` | HTTP only; playground requests also delegate to workers |
| `worker` | `python worker.py` | Judge consumers without an HTTP listener |

The Docker entrypoint selects the command from `GRADER_ROLE` and uses `PORT` for HTTP. Separate roles require database mode and an explicit stable `SESSION_SECRET`. All API replicas must share that secret. Each deployment needs the same problem packages, engine version, and appropriate Supabase environment. Keep the service-role key server-side.

## Host capacity

Defaults derive from cgroup CPU/RAM limits. The scheduler reserves interpreter/API memory and budgets each active job for its supervisor plus the larger compiler/test address-space ceiling, including the enabled languages’ memory multipliers and virtual headroom. It caps requested worker count to both CPU and memory capacity. `GRADER_JUDGE_JOBS` no longer enables concurrent tests: effective test parallelism is one. Large worker/memory overrides cannot override the host safety calculation. Inspect `/api/version` for effective values.

Production has four separate one-CPU / one-GiB worker units and a four-CPU /
four-GiB judge slice. Each unit runs one job with serial tests. Its private tmpfs
is 128 MiB / 4,096 inodes; each job also receives a cgroup ceiling of 640 MiB,
64 tasks, one CPU and no swap. The API has a separate one-CPU / 320 MiB budget.
Declared problem memory is capped at 128 MiB with existing language allowances;
overall judging timeout is 240 seconds.

Exactly four jobs can execute at once; the durable admission cap allows 25 more
queued jobs. Each ordinary account has at most one queued/running job. Queue
capacity is a backlog bound, not a measured simultaneous-user capacity.

File locks coordinate processes sharing one resource allocation. The production
worker units have separate allocations and separate
`GRADER_CAPACITY_DIR=/var/lib/newbiegrader/capacity/worker-N` directories. Sharing
one single-slot directory would serialize the whole fleet. Their durable UID
ledger remains shared and append-only. Independent hosts have separate capacity
budgets and coordinate job ownership through database leases. Never enable two
fleets during a host cutover without the reviewed fencing/drain workflow.

## Browsing latency and compile warmup

The API serves the problem catalog from an in-process snapshot. A TTL refresh
runs on a daemon thread and leaves the current snapshot readable while the
Supabase query is in flight. The refresh selects the lean metadata columns
(without `statement_md`); unchanged rows keep their cached statement, while a
changed or no-PDF row fetches its statement once on detail. No refresh downloads
problem files.

Problem browsing requires a verified website session. Member API responses,
including statements and PDFs, use `private, no-store`; there is no shared/CDN
cache or persistent HTTP cache of those responses. The SPA deduplicates detail
and sample requests in memory and clears them on identity changes. PDF frames
load on opening a problem, without speculative hover downloads. Guests fetch
only bounded landing previews through `/api/home`.

Build the optional C++ PCH as root after a compiler/toolchain update:

```sh
sudo /usr/local/lib/newbiegrader/venv/bin/python tools/build_cxx_pch.py
```

The default destination is `/usr/local/lib/newbiegrader/pch/cxx`, which is
readable by the Landlocked compile sandbox. The grader adds `-I` only when
`bits/stdc++.h.gch` exists; a missing or rejected PCH falls back to a normal
compile. Rebuild the PCH whenever `g++` or its headers change, and remove it if
the toolchain becomes incompatible.

## Submission latency

Prepare trusted Python bytecode before each release with
`deploy/host/precompile_runtime.py` and the dedicated Python 3.13 venv. Workers
attempt the next durable claim immediately after completion; idle polling backs
off to one second and outages to 30 seconds. Browser polls for active/head-of-queue
jobs cap their scheduled interval at one second, while deeper queues retain
backoff. Network request time is additional to the scheduled polling interval.

On the same synthetic 17-case workload, precompilation first reduced worker
wall time from 7.6–8.3 seconds to 3.4–3.8 seconds. The current isolated runner
imports `judge_execution.py`, which uses the canonical grader without loading
the HTTP/database stack. Execution settings are passed explicitly; feedback,
limit and isolation logic are preserved. Linux sampling avoids launching `ps`
after the supervised process is gone or has no resident address space.

A fresh comparison measured 3.51–3.85 seconds before and 2.82–3.03 seconds after
these changes, with the same one-CPU/one-GiB allocation and serial cases. Public
Python/C/C++ checks measured 2.90–3.28 seconds in the worker and 3.75–4.81 seconds
from submission to observed result. One C request had a longer observed result
wait despite faster worker execution; network and polling variance still matter.
See the [before/after record](releases/pc-execution-performance-2026-10-22.json).

These are scoped short-job measurements. The eight-job fleet probe confirms four
overlapping workers; neither probe establishes sustained capacity for 30–50
users. Rough classroom planning estimates still require a representative load
test. Heavy solutions and backlogs retain their actual execution/queue time.

## Rollout order

1. Complete the [security release checks](SECURITY_READINESS.md) and native host checks on Linux. Synthetic CI and other-platform checks do not replace actual host isolation tests.
2. Trial the new revision against a disposable database and test accounts. Build the image, run all checks, and exercise both local and database modes.
3. For a local-to-database transition, stop admissions, drain jobs if practical, and stop all old local-mode processes. Have the maintainer apply the complete idempotent `supabase/schema.sql` in Supabase SQL Editor. Do not run a mixed local/database fleet. Legacy running rows without ownership receive an expired lease so database workers can reclaim them; live leased ownership is left alone.
4. For the current host, follow the four-instance activation in the host runbook. Verify health/version, admission, judging, history, ownership and redaction before opening service. API-only mode requires the qualified worker fleet.
5. Deploy the backend before frontend changes that depend on it. Verify effective limits and watch queue wait, errors, memory, and throughput. Change allocations only after repeating native qualification and measuring the resulting capacity.

To roll back queue mode, stop every database consumer and resolve/drain in-flight jobs first. Do not enable local recovery while database workers are alive.

## Measuring load

`tools/load_test.py` uses the standard library and requires explicit `--execute --test-installation`. Its default target is loopback; remote installations require `--allow-remote`. Use a disposable test database and accounts, never production credentials. Existing test accounts can be supplied through `NG_LOAD_ACCOUNTS`; `--register` creates throwaway accounts on the test installation.

```sh
python tools/load_test.py --execute --test-installation --register \
  --target http://127.0.0.1:8094 --users 4 --bursts 2 \
  --job-timeout 90 --playground-every 3
```

The JSON report includes API latency percentiles, admitted/completed attempts, throughput, HTTP/error counts, and submission queue/service durations. Timings use server timestamps where available and label polling estimates otherwise. Read completion counts alongside latency; quick rejected requests do not establish judging throughput. Use host/container monitoring for peak RSS, throttling, and OOM events.

The committed CI workflow includes normal Python/Node checks plus a disposable PostgreSQL job that applies the migration twice and exercises real SQL admission, quotas, leases, fencing, priority, and completion behavior. Running CI remotely and Linux deployment validation remain separate from local checks.
