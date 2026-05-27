#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys

# Force UTF-8 on stdio before anything prints. When Electron spawns this
# script (UI-source/electron/main.js:195), stdio is a pipe, not a TTY,
# so Python falls back to locale.getpreferredencoding(False) → cp1252 on
# default Western Windows (ACP=1252). Metadata JSON can contain any
# non-ASCII char (titles, author names, summaries) — cp1252 chokes on
# anything outside Latin-1, crashing the IPC mid-response. Mirrors the
# top-of-file block in aio-dl.py / aio_search_cli.py.
# Grep: UnicodeEncodeError reconfigure
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
try:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from metadata_editor import read_metadata, update_metadata


def main() -> None:
    parser = argparse.ArgumentParser("metadata editor CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    read_p = sub.add_parser("read")
    read_p.add_argument("path")
    write_p = sub.add_parser("update")
    write_p.add_argument("path")
    write_p.add_argument("--cover-path", default=None)
    args = parser.parse_args()

    if args.command == "read":
        print(json.dumps(read_metadata(args.path), ensure_ascii=False))
        return

    data = json.loads(sys.stdin.read() or "{}")
    update_metadata(args.path, data, args.cover_path)
    print(json.dumps({"ok": True}))


if __name__ == "__main__":
    main()
