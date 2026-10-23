# Upgrading the host to Python 3.13

This moves an existing host from Python 3.11.17 to 3.13.17 and applies the
matching dependency pins. A fresh host needs nothing here: `bootstrap.sh`
already installs 3.13. Re-running `bootstrap.sh` on an existing host does **not**
upgrade it, because it only creates the venv when none exists.

## What changes

- Python 3.11.17 to 3.13.17, and the bootstrap `uv` from 0.13.1 to 0.13.5. The
  versions are set once in `deploy/host/bootstrap.sh`; the upgrade script reads
  them from there.
- `fastapi` 0.143.3 to 0.161.1, `starlette` 1.8.0 to 1.9.0, `uvicorn` 0.34.0 to
  0.54.0 and `certifi` to 2026.10.23, in `requirements.txt` and
  `deploy/host/requirements.lock`.
- Nothing else: units, paths, credentials, the UID ledger and problem files are
  untouched. The venv stays at `/usr/local/lib/newbiegrader/venv`, which keeps
  the interpreter inside the Landlock-allowed `/usr` tree.

Behavior changes that could matter, checked against this code base:

- FastAPI 0.147 to 0.150 treat empty form fields and unselected file inputs as
  missing. Only the two submission endpoints use `Form`/`File`; an empty
  `time_limit` now falls back to its default instead of failing with 422.
- Uvicorn 0.48 ignores duplicate forwarding headers. The API runs with
  `--no-proxy-headers`, so client IP handling is unchanged. Confirm that with
  the health check below.
- Python submissions run on the interpreter the grader uses, so contestants'
  code also moves to 3.13. The modules removed in 3.12/3.13 (`distutils`, `imp`,
  `asyncore`, `cgi`, `telnetlib` and similar) are the only visible difference
  for typical solutions.

## Before you start

1. Install the new release into `/opt/newbiegrader/releases/<release>` as usual
   (reviewed snapshot, no credentials), so it contains the new
   `requirements.lock`, `precompile_runtime.py` and `upgrade_python.sh`.
2. Confirm CI passed on the release commit, including `pip-audit` and the
   critical gate.
3. Take a fresh backup (`newbiegrader-backup.service`) and note the current
   `current` release symlink target for rollback.

## Upgrade

Run as root on the host.

1. Stop admission and drain: set `GRADER_ACCEPT_JOBS=0` in the API and worker
   configuration, remove `/etc/newbiegrader/worker-enabled`, and wait until no
   job is queued or running and no lease is live.
2. Stop the services:

   ```sh
   systemctl stop newbiegrader-api.service newbiegrader-worker@{1,2,3,4}.service \
       newbiegrader-discord-presence.service
   ```

3. Run the script with the new release:

   ```sh
   /opt/newbiegrader/releases/<release>/deploy/host/upgrade_python.sh \
       /opt/newbiegrader/releases/<release>
   ```

   It refuses to run while any listed unit is active. It installs the pinned
   `uv`, installs Python 3.13.17 beside 3.11, moves the old venv to
   `venv.previous`, builds the new venv at the original path from the release
   lock, precompiles the runtime and checks imports and CA loading as the
   unprivileged API user. If any step fails, the old venv is restored and the
   script exits non-zero. It never starts a service.
4. Point `/opt/newbiegrader/current` at the new release if it does not already.
5. Verify before opening admission:

   ```sh
   deploy/host/verify-workers.sh
   deploy/host/verify-recovery.sh
   ```

   Then run the native kernel canaries and the Linux critical gate from the
   [deployment guide](HOST_DEPLOYMENT.md). A skip or missing cleanup is a failure.
6. Start the API, check `/api/health` locally and through the public URL, then
   create `/etc/newbiegrader/worker-enabled`, enable instances 1 to 4, set
   `GRADER_ACCEPT_JOBS=1` and open admission.
7. Submit one real Python, C and C++ solution and confirm the verdicts and
   linked history.

Keep `venv.previous` and the 3.11 interpreter under
`/usr/local/lib/newbiegrader/python` until the host has run normally for a week,
then remove them.

## Rollback

With services stopped and drained again:

```sh
/opt/newbiegrader/releases/<release>/deploy/host/upgrade_python.sh --rollback
```

This swaps `venv.previous` back into place and keeps the upgraded venv as
`venv.failed` for inspection. Point `current` back at the previous release,
because the old `precompile_runtime.py` only accepts 3.11, then repeat steps 5
to 7. Rollback needs the previous release's dependency lock to match
`venv.previous`, which it does unless the venv was changed by hand.

## Troubleshooting

- `Stop these units and drain jobs first`: a unit in the script's list is still
  active. Stop it; do not edit the script.
- `$PREVIOUS exists`: a prior upgrade left `venv.previous`. Roll back, or remove
  it deliberately once you no longer need that rollback.
- Every sandboxed job fails with "privilege drop failed": the interpreter is
  outside `/usr`. The venv must stay under `/usr/local`.
- `precompile_runtime.py` says to use the dedicated project Python: it is
  running under the wrong interpreter or the venv is not at the expected path.
