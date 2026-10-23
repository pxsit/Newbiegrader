#!/bin/sh
# Load deploy/host/pg_hba.conf.example and pg_ident.conf.example into a throwaway
# PostgreSQL cluster and fail if the server reports a parse error in either file.
# Run as an unprivileged user that may run initdb (e.g. the postgres OS user).
set -eu
HOST_DIR=$(cd "$(dirname "$0")/../deploy/host" && pwd)
BIN=$(ls -d /usr/lib/postgresql/*/bin | sort -V | tail -1)
DIR=$(mktemp -d)
trap '"$BIN/pg_ctl" -D "$DIR/data" -m immediate stop >/dev/null 2>&1 || true; rm -rf "$DIR"' EXIT
mkdir "$DIR/sock"
"$BIN/initdb" -D "$DIR/data" -A peer >/dev/null
cp "$HOST_DIR/pg_hba.conf.example" "$DIR/data/pg_hba.conf"
cp "$HOST_DIR/pg_ident.conf.example" "$DIR/data/pg_ident.conf"
"$BIN/pg_ctl" -D "$DIR/data" -o "-k $DIR/sock -c listen_addresses= -p 5499" -l "$DIR/log" -w start >/dev/null
q() { psql -h "$DIR/sock" -p 5499 -d postgres -Atc "$1"; }
test "$(q 'select count(*) from pg_hba_file_rules where error is not null')" = 0
test "$(q 'select count(*) from pg_ident_file_mappings where error is not null')" = 0
# The OS user running this script has no mapping, so the application roles refuse it.
if psql -h "$DIR/sock" -p 5499 -U newbiegrader_api -d newbiegrader -c 'select 1' >/dev/null 2>&1; then
  echo "unmapped OS user reached an application role" >&2
  exit 1
fi
echo "pg auth config OK"
