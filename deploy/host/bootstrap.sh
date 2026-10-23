#!/bin/sh
# Run as root on an authorized Ubuntu host; no secrets as arguments.
set -eu
test "$(id -u)" = 0
. /etc/os-release
test "$ID" = ubuntu
case "$VERSION_ID" in 24.04|26.04) ;; *) echo 'Unsupported Ubuntu release' >&2; exit 1 ;; esac
test "$(uname -m)" = x86_64
export DEBIAN_FRONTEND=noninteractive
# Prevent the package's default public welcome server from starting.
if ! command -v nginx >/dev/null 2>&1; then systemctl mask nginx.service; fi
apt-get update -q
apt-get install -y --no-install-recommends python3-venv gcc g++ nginx curl ca-certificates rsync age
id newbiegrader-api >/dev/null 2>&1 || useradd --system --home /nonexistent --shell /usr/sbin/nologin newbiegrader-api
id newbiegrader-tunnel >/dev/null 2>&1 || useradd --system --home /nonexistent --shell /usr/sbin/nologin newbiegrader-tunnel
id newbiegrader-presence >/dev/null 2>&1 || useradd --system --user-group --no-create-home --home /nonexistent --shell /usr/sbin/nologin newbiegrader-presence
install -d -m 0755 /opt/newbiegrader/releases /usr/local/lib/newbiegrader /var/lib/newbiegrader
install -d -m 0700 /etc/newbiegrader /var/lib/newbiegrader/sandbox-identities /var/lib/newbiegrader/capacity
install -d -m 0755 /var/lib/newbiegrader/workspaces /etc/systemd/journald.conf.d
python3 -m venv /usr/local/lib/newbiegrader/bootstrap
/usr/local/lib/newbiegrader/bootstrap/bin/pip install --disable-pip-version-check 'uv==0.13.5'
export UV_PYTHON_INSTALL_DIR=/usr/local/lib/newbiegrader/python
/usr/local/lib/newbiegrader/bootstrap/bin/uv python install 3.13.17
if ! test -x /usr/local/lib/newbiegrader/venv/bin/python; then
    /usr/local/lib/newbiegrader/bootstrap/bin/uv venv --python 3.13.17 /usr/local/lib/newbiegrader/venv
fi
cat > /etc/systemd/journald.conf.d/60-newbiegrader-bounds.conf <<'EOF'
[Journal]
SystemMaxUse=200M
RuntimeMaxUse=32M
MaxRetentionSec=7day
RateLimitIntervalSec=30s
RateLimitBurst=1000
EOF
systemctl restart systemd-journald
# Application listeners are loopback-only. Preserve the host's existing firewall
# and Tailscale policies; do not change SSH access during unattended bootstrap.
printf '%s\n' 'Bootstrap complete; application services have not been started.'
