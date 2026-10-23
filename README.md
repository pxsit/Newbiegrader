<div align="center">

# Newbiegrader

**An online judge for Thai competitive-programming instruction.**

Strict limits · Undisclosed tests · Asynchronous judging · Contests · Teacher tooling

<br>

[![License](https://img.shields.io/badge/license-AGPL--3.0--or--later-blue)](LICENSE)
![Python](https://img.shields.io/badge/python-3.11-3776AB?logo=python&logoColor=white)
![Problems](https://img.shields.io/badge/problems-89-brightgreen)
![Test cases](https://img.shields.io/badge/test%20cases-1594-brightgreen)
![Grader](https://img.shields.io/badge/grader-v2.0.7-informational)
![Frontend](https://img.shields.io/badge/frontend-no%20build%20step-orange)

[Live Demo](https://newbiegrader.pages.dev) · [Report a Bug](../../issues) · [Discord](https://discord.gg/7NQDHFBpf6) · [Documentation](docs)

</div>

---

## Table of Contents

- [About](#about)
- [Features](#features)
- [Verdicts](#verdicts)
- [Architecture](#architecture)
- [Design Principles](#design-principles)
- [Stack](#stack)
- [Contributing](#contributing)
- [Team](#team)
- [License](#license)

## About

Newbiegrader is a web-based programming judge designed for teaching competitive programming in Thai. Students submit solutions in **Python 3**, **C (C11)** or **C++ (C++17)** and receive automatic verdicts under real time and memory limits, the same way they would on a professional contest platform.

Guests can view the landing page and use login, registration and recovery.
Opening problems, contests, history and other member pages requires login;
the API enforces the same boundary, including statement PDFs and samples.

Practice submissions show input, expected output and actual output for failed cases to their owner. Contest submissions keep hidden tests private; upcoming/running contest problems also withhold extra feedback through the practice path. Previously disclosed practice data cannot be made secret again by reusing it in a contest. See the [feedback policy](docs/submission-feedback-policy.md).

<div align="center">

| 89 | 1,594 | v2.0.7 | 3 |
| :---: | :---: | :---: | :---: |
| problems | test cases | grader engine | languages |

</div>

The catalog contains 89 problems, numbered by a calibrated difficulty order under an explicit operator-approved migration. Every problem carries a `level` (1–5) that reflects the technique it actually demands, and the coarse easy/medium/hard badge is derived from it. Stable slug IDs and submission history are preserved; the [number map](docs/catalog/problem-numbers-2026-10-22.csv) records previous numbers.

Private authoring packages contain reference solutions. Recorded verdicts identify the test set and grader version; private fixtures and references are excluded from this public repository.

## Features

### Strict and honest limits
Wall-clock timeouts, measured peak memory and bounded execution. The production Linux host uses private job identities, Landlock filesystem rules, seccomp network endpoint denial, per-job cgroups and bounded workspaces. Each test reports its own time and memory figures; see the [security checks and accounting limits](docs/SECURITY_READINESS.md).

### Undisclosed tests
Contest hidden tests return verdicts and resource figures. In practice, an owner can inspect input, expected output and actual output for failed cases. Upcoming/running contest problems withhold that extra practice feedback. The same policy applies to responses, history and persisted results.

### Asynchronous judging
Submissions return a job ID for polling. With `GRADER_QUEUE_MODE=database`, admission is durable and completion/history commit together under a worker lease. The default `local` mode uses an in-memory queue with best-effort persistence; it cannot guarantee recovery after a restart or database outage. Database workers back off while idle and during outages to reduce unnecessary RPC traffic.

### Contests
Timed Codeforces-style rounds with registration, frozen scoreboards and organizer announcements. Contest-only problems stay out of the public task list. Practice statistics exclude contest submissions.

### Teacher tooling
Submission history, per-problem statistics, plagiarism screening, one-click rejudging, problem reporting and an admin overview with queue monitoring.

### Lightweight frontend
Plain HTML, CSS and JavaScript. No bundler, no transpiler and no framework.

## Verdicts

| Verdict | Meaning |
| :---: | --- |
| `AC` | Accepted. Every test passed. |
| `WA` | Wrong answer. |
| `TLE` | Time limit exceeded. |
| `MLE` | Memory limit exceeded. |
| `OLE` | Output limit exceeded. |
| `RE` | Runtime error. |
| `CE` | Compilation error. |

## Architecture

| Component | Responsibility |
| --- | --- |
| `grader.py` | The single judging engine, built on the Python standard library only |
| `server.py` | FastAPI application: authentication, job queue, quota and visibility policy, result redaction |
| `judge_execution.py` · `judge_runner.py` · `worker.py` | Supervised bounded execution and the dedicated worker role |
| `static/` | The web interface |
| `problems/` | Local working copy of the problem set (configuration, statement, tests). Not versioned here — runtime files are published to protected backend storage; metadata lives in Supabase |
| `supabase/` | Database schema with append-only, re-runnable migrations |

Host capacity is read from the service or container at startup, and worker counts, memory ceilings, queue bounds and timeouts are derived from it automatically. Current production is Cloudflare Pages + an Ubuntu PC with four isolated judge workers. Problem PDFs, public samples and private tests are served from backend disk (`GRADER_FILE_STORAGE=local`); Supabase holds metadata, auth and the durable queue. See [current architecture](docs/PRODUCTION_ARCHITECTURE.md), [backend file storage](docs/BACKEND_PROBLEM_FILES.md), and the [host runbook](docs/HOST_DEPLOYMENT.md).

The four workers execute four jobs concurrently, with up to 25 additional
queued jobs. The API has one CPU / 320 MiB; judging has a separate four-CPU /
four-GiB budget and a 128 MiB declared-memory cap. For the measured 17-case
short-job workload, trusted runtime precompilation first reduced worker time
from about eight seconds to 3.4–3.8 seconds. Removing web-stack startup and
redundant exited-process sampling reduced the same synthetic workload by a
further 20%, to 2.82–3.03 seconds. Public Python/C/C++ checks took 3.75–4.81
seconds from submit to observed result. These scoped measurements do not
establish all-problem latency or simultaneous-user capacity. See
[load handling](docs/load-handling.md) and the
[current measurement record](docs/releases/pc-execution-performance-2026-10-22.json).

The current [public website security qualification](docs/PUBLIC_SECURITY.md)
records the browser protections, sandbox restrictions, patched CA bundle,
edge rate limits and Turnstile email-request gate, together with measured live
checks, revocable database sessions and retained SSH/OS limitations. Worker
database traffic uses
pooled HTTP/1.1 to avoid the observed HTTP/2 connection-turnover failures;
ambiguous queue claims are never automatically replayed.

## Design Principles

1. **One judging engine.** Every verdict flows through the same grader entry points, and no judging logic is duplicated in request handlers.
2. **Controlled feedback.** Every result passes through the practice/contest disclosure policy before it reaches a client and before it is stored.

## Stack

| Layer | Technology |
| --- | --- |
| Judging engine | Python (standard library only) |
| API | FastAPI |
| Database | PostgreSQL via Supabase |
| Frontend | HTML, CSS and JavaScript on Cloudflare Pages |
| Production backend | Ubuntu PC, Python 3.11, separate systemd API and four worker services |
| Local packaging | Docker |

## Contributing

For problem authors, start with the [shared authoring workflow](docs/making-problems.md) and versioned templates in `tools/templates/problem/`.

Contributions are welcome. You can suggest features or report bugs by opening an issue, chat with the community on [Discord](https://discord.gg/7NQDHFBpf6), or submit a pull request with a clear description of the change. Please report security issues through the maintainer's profile rather than a public issue.

## Team

| Role | Person | Scope |
| --- | --- | --- |
| Maintainer | [@n3wbiekkkk](https://github.com/n3wbiekkkk) | Judging engine, API, frontend, problem set, deployment |
| Contributor | [@Piyaph4t](https://github.com/Piyaph4t) | Judge-worker split, queue test coverage |

## License

Copyright (C) 2026 Newbiegrader contributors. Source code: <https://github.com/n3wbiekkkk/Newbiegrader>.

### Code

This program is free software, released under the **GNU Affero General Public License v3.0 or later** (`AGPL-3.0-or-later`). The full license text is in [LICENSE](LICENSE).

Under AGPL §13, running a modified version for users over a network requires offering them the Corresponding Source. For this deployment, the source is available at <https://github.com/n3wbiekkkk/Newbiegrader>.

### Name and logo

The Newbiegrader name and logo are **not** covered by the code license. All rights reserved.

### Third-party typefaces

Statement PDFs embed typefaces that keep their own licenses and are not covered by the licenses above: Anuphan and Inconsolata under SIL OFL 1.1, Latin Modern Roman under the GUST Font License, and TH Sarabun New under GPL-2.0-or-later with the font-embedding exception (so the rendered PDFs are not covered by the GPL). See `tools/fonts/`.

No warranty is provided and no trademark rights are granted.
