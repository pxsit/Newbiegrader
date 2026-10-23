#!/usr/bin/env python3
"""Prepare trusted Python bytecode before starting a reviewed host release."""
from __future__ import annotations

import argparse
import compileall
import os
from pathlib import Path
import site
import sys
import sysconfig


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path, default=Path('/opt/newbiegrader/current'))
    args = parser.parse_args()
    if os.geteuid() != 0 or sys.version_info[:2] != (3, 13):
        raise SystemExit('Run as root with the dedicated project Python 3.13')
    prefix = Path('/usr/local/lib/newbiegrader')
    if Path(sys.prefix).resolve() != prefix / 'venv':
        raise SystemExit('Refusing to modify another Python installation')
    release = args.release.resolve(strict=True)
    if release.parent != Path('/opt/newbiegrader/releases'):
        raise SystemExit('Release must be directly inside the project releases directory')
    if any((release / name).exists() for name in ('problems', 'examples', '.env', 'agent.md')):
        raise SystemExit('Release must contain public source only')
    targets = [Path(sysconfig.get_path('stdlib')).resolve(),
               *(Path(p).resolve() for p in site.getsitepackages()), release]
    for folder in targets:
        if folder != release and not folder.is_relative_to(prefix):
            raise SystemExit('Refusing a runtime outside the project installation')
        for parent in (folder, *folder.parents):
            info = parent.stat()
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise SystemExit('Runtime paths must be root-controlled')
    # Sandbox interpreters also read the public stdlib caches. Workers keep
    # PYTHONDONTWRITEBYTECODE=1 and cannot replace these root-owned files.
    old_mask = os.umask(0o022)
    try:
        for folder in targets:
            if not compileall.compile_dir(folder, quiet=1, workers=1,
                                          limit_sl_dest=str(folder)):
                raise SystemExit('Runtime bytecode preparation failed')
    finally:
        os.umask(old_mask)
    print('Prepared trusted Python 3.13 runtime and release bytecode')


if __name__ == '__main__':
    main()
