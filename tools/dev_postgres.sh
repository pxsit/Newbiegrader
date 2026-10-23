#!/bin/sh
# Start a disposable PostgreSQL 18 for the database tests and print the DSN.
#   eval "$(tools/dev_postgres.sh)" && python -m unittest tests.test_db
# Needs Docker. Without Docker, point NEWBIEGRADER_TEST_DSN at any PostgreSQL 18
# superuser connection (WSL: host=/var/run/postgresql user=postgres dbname=postgres).
set -e
docker rm -f newbiegrader-test-pg >/dev/null 2>&1 || true
docker run -d --name newbiegrader-test-pg -e POSTGRES_PASSWORD=postgres -p 55432:5432 postgres:18 >/dev/null
until docker exec newbiegrader-test-pg pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done
echo "export NEWBIEGRADER_TEST_DSN='postgresql://postgres:postgres@127.0.0.1:55432/postgres'"
