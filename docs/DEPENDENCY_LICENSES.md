# Dependency licences

Audit date: 2026-10-21; certifi security-version update verified 2026-10-22. Scope: everything this repository ships or links —
Python packages, LaTeX packages, fonts, container base images, OS packages and
third-party images.

Newbiegrader's own code is **AGPL-3.0-or-later** (see [LICENSE](../LICENSE)).
Every dependency below is used as an unmodified library or asset, so each must
be compatible with distributing the combined work under AGPL-3.0-or-later.

## Python packages

`requirements.txt` pins the direct dependencies; `deploy/host/requirements.lock`
pins the full production tree (uv-generated). Licence data comes from the
PyPI JSON API for the exact pinned version.

### Direct dependencies (`requirements.txt`)

| Package | Version | Licence | Compatible with AGPL-3.0-or-later |
| --- | --- | --- | --- |
| fastapi | 0.143.3 | MIT | Yes (permissive) |
| starlette | 1.8.0 | BSD-3-Clause | Yes (permissive) |
| uvicorn | 0.34.0 | BSD-3-Clause | Yes (permissive) |
| python-multipart | 0.0.32 | Apache-2.0 | Yes (permissive) |
| supabase | 2.7.1 | MIT | Yes (permissive) |
| itsdangerous | 2.2.0 | BSD-3-Clause | Yes (permissive) |
| certifi | 2026.10.19 | MPL-2.0 | Yes (unmodified, file-level copyleft) |

### Full production tree (`deploy/host/requirements.lock`)

| Package | Version | Licence | Verdict |
| --- | --- | --- | --- |
| annotated-doc | 0.0.5 | MIT | permissive |
| annotated-types | 0.8.0 | MIT | permissive |
| anyio | 4.15.1 | MIT | permissive |
| certifi | 2026.10.19 | MPL-2.0 | weak copyleft, file-level |
| click | 8.5.0 | BSD-3-Clause | permissive |
| deprecation | 2.1.0 | Apache-2.0 | permissive |
| fastapi | 0.143.3 | MIT | permissive |
| gotrue | 2.12.4 | MIT | permissive |
| h11 | 0.16.0 | MIT | permissive |
| h2 | 4.4.1 | MIT | permissive |
| hpack | 4.2.0 | MIT | permissive |
| httpcore | 1.0.9 | BSD-3-Clause | permissive |
| httpx | 0.27.2 | BSD-3-Clause | permissive |
| hyperframe | 6.1.0 | MIT | permissive |
| idna | 3.20 | BSD-3-Clause | permissive |
| itsdangerous | 2.2.0 | BSD-3-Clause | permissive |
| opentelemetry-api | 1.45.1 | Apache-2.0 | permissive |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | permissive (dual) |
| postgrest | 0.16.11 | MIT | permissive |
| pydantic | 2.14.1 | MIT | permissive |
| pydantic-core | 2.50.1 | MIT | permissive |
| pyjwt | 2.15.1 | MIT | permissive |
| python-dateutil | 2.9.0.post0 | Apache-2.0 OR BSD-3-Clause | permissive (dual; the wheel ships the BSD-3-Clause text) |
| python-multipart | 0.0.32 | Apache-2.0 | permissive |
| realtime | 2.33.0 | MIT | permissive (wheel ships no LICENSE file; PyPI metadata and trove classifier agree on MIT) |
| six | 1.17.0 | MIT | permissive |
| sniffio | 1.3.1 | MIT OR Apache-2.0 | permissive (dual; the wheel ships both texts) |
| starlette | 1.8.0 | BSD-3-Clause | permissive |
| storage3 | 0.7.7 | MIT | permissive |
| strenum | 0.4.15 | MIT | permissive |
| supabase | 2.7.1 | MIT | permissive |
| supafunc | 0.5.1 | MIT | permissive |
| typing-extensions | 4.16.0 | PSF-2.0 | permissive |
| typing-inspection | 0.4.4 | MIT | permissive |
| uvicorn | 0.34.0 | BSD-3-Clause | permissive |
| websockets | 15.0.1 | BSD-3-Clause | permissive |

**Result:** no GPL-only, LGPL or proprietary Python dependency. Nothing
conflicts with AGPL-3.0-or-later.

`certifi` (MPL-2.0) is the only non-permissive entry. MPL-2.0 is a file-level
copyleft: it obliges you to publish modifications to *certifi's own files*,
not to the work that merely links it. `certifi` is used unmodified, so there is
no obligation and no conflict. The Mozilla CA bundle it ships is separately
covered by the MPL-2.0 licence of that project.

## JavaScript

There is no `package.json`, no bundler and no npm dependency. `static/`,
`functions/` and `tools/` use only the browser and Node standard libraries, so
no third-party JavaScript licence applies.

## LaTeX packages

`tools/make_statement_tex.py` builds the statements with tectonic (a XeTeX
engine). The preamble loads `fontspec`, `graphicx`, `xltxtra`, `geometry`,
`xcolor`, `amsmath`, `longtable`, `array`, `needspace` and `fancyhdr`. All are
LaTeX Project Public Licence (LPPL 1.3c) or permissively licensed; LPPL is
compatible with AGPL. The bundled `inconsolata` package ships the OFL-1.1
Inconsolata fonts listed below.

## Bundled fonts (`tools/fonts/`)

These are third-party assets with their own licences. They are **not** covered
by this repository's AGPL grant.

| Font | Licence | Note |
| --- | --- | --- |
| TH Sarabun New (`thsarabunnew*.ttf`) | GPL-2.0-or-later **with the font-embedding exception** | Verified from the font's own name table ID 13. The embedding exception means the rendered PDFs are not covered by the GPL; only the font files are. GPL-2.0-or-later material is compatible with AGPL-3.0-or-later. |
| Anuphan (`anuphan-*.ttf`) | SIL OFL 1.1 | Static instances of the upstream variable font; see `tools/fonts/ANUPHAN-NOTE.md` and `OFL-anuphan.txt`. |
| Inconsolata (`inconsolata-*.ttf`) | SIL OFL 1.1 | See `OFL-inconsolata.txt`. |
| Latin Modern Roman (`lmroman-*.otf`) | GUST Font License (LaTeX Project) | See `GUST-FONT-LICENSE.txt`. |

The OFL fonts require the licence text to ship with them; it does, in
`tools/fonts/`.

## Container base image and OS packages

| Component | Licence | Note |
| --- | --- | --- |
| `python:3.13-slim` (Dockerfile) | PSF-2.0 | Permissive. |
| `gcc`, `g++` (Dockerfile, `deploy/host/bootstrap.sh`) | GPL-3.0-with-GCC-exception | Used as unmodified toolchain binaries; the runtime exception permits linking. Separate processes, never linked into the AGPL work. |
| `nginx` (`deploy/host/bootstrap.sh`) | BSD-2-Clause | Reverse proxy only; runs as a separate service. |
| `python3-venv`, `curl`, `ca-certificates`, `rsync` | PSF-2.0 / curl / MPL-2.0 / GPL-3.0 | Unmodified OS tooling, separate processes. |

## Third-party images

| Asset | Licence | Note |
| --- | --- | --- |
| `static/google-g.png` | Google brand guidelines, not an open-source licence | Unmodified Google G asset. Not project artwork and not covered by the AGPL grant; see `static/google-g.SOURCE.md`. |
| `static/logo*.png`, `static/icons/*.png` | Project artwork | Not covered by the AGPL code licence; all rights reserved. |

## Method

Licence data was taken from the PyPI JSON API for each **exact pinned
version** (`https://pypi.org/pypi/<name>/<version>/json`), preferring the
PEP 639 `license_expression` field, then the `License ::` trove classifiers.
Where PyPI only said "BSD License" / "MIT License" / "Apache Software
License", the wheel was downloaded and its `LICENSE` file read to resolve the
exact SPDX identifier. Fonts were resolved from the `name` table (ID 13/14) of
the bundled font files themselves.

## Summary

There is **no dependency whose licence conflicts with AGPL-3.0-or-later**:
every Python, JavaScript and LaTeX dependency is permissive or LPPL, and the
single weak-copyleft entry (`certifi`, MPL-2.0) is used unmodified. The only
GPL-family material is TH Sarabun New, whose font-embedding exception keeps the
rendered PDFs outside the GPL.

No dependency was changed to produce this audit.
