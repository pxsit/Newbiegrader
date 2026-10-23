# Problem browsing and PDF caching

Production uses Supabase metadata and protected local file bytes. Opening a
statement PDF resolves metadata and checks visibility before reading the selected
public file root. Browsing never loads the private suite. Claimed submissions
use the full hash-verified loader; local-file mode retains no full-suite cache.
See [file storage](BACKEND_PROBLEM_FILES.md).

## Browser and server caches

The public landing uses `/api/home`, which returns at most three problem
previews per difficulty and aggregate submission totals. Full catalogs, details,
samples, PDFs, statistics, contests and submission endpoints require a verified
website session. Registration, login and recovery remain accessible to guests.
Health/version/language diagnostics remain public for monitoring.

All member API responses use `private, no-store`, including denials and
conditional PDF responses. This overrides the handlers' legacy freshness hints
so HTTP caches cannot reopen member content after logout. Existing downloaded
bytes cannot be retracted. Identity changes invalidate the SPA's in-memory
caches and delayed responses; local catalog data is read only after login.
PDF hover downloads are disabled because they cannot warm a no-store cache.

The bounded server PDF cache expires after 300 seconds. Metadata refresh normally
runs every 30 seconds and invalidates a PDF when `updated_at` or
`statement_pdf_path` changes, or the problem disappears. Publish new PDF bytes
and their matching metadata together under the paused/drained publication
workflow. Replacing bytes alone can leave a previous cached PDF until expiry.
Legacy Storage-mode readers remain for explicit recovery, not production fallback.

The SPA shares in-flight detail requests between hover/navigation and retains
details for 60 seconds. That client cache does not authorize another PDF, sample
or submission request; those endpoints apply their own access policy.

## Regression checks

These tests use synthetic fixtures and fake Supabase, without a live database:

```sh
python -m unittest discover -s tests -p test_problem_loading.py -v
node --test tests/test_problem_loading.js
```

They cover zero hidden-file downloads while browsing, numeric resolution,
authorization before conditional responses, PDF version/TTL invalidation,
full-suite loading for judging, client request deduplication, expiry/retry,
identity changes and stale navigation responses. Cache/privacy and protected-file
critical tests remain part of the full release gates.

Compare latency from the same browser/network and URLs. Record warm/cold state,
status, bytes transferred and request counts. The startup optimization and
measured submission latency are documented in [the host runbook](HOST_DEPLOYMENT.md).
