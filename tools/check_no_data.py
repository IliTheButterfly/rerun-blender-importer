#!/usr/bin/env python3
"""Refuse to let recorded data into this repository.

This add-on's whole subject matter is recordings, which makes it dangerously
easy to commit one — a sample .rrd, a screenshot of a map, a .blend saved after
an import. Recordings and anything rendered from them belong to whoever flew
them, and a repository is exactly the wrong place for that: history is forever,
"private" is a permission setting that can be flipped or forked, and a
force-push does not erase anything (the old commits stay reachable by SHA).

So this refuses, mechanically:

* data and media file types, whatever they are named;
* files big enough to be data even if the extension looks innocent;
* absolute paths from someone's machine, which leak layout and identity;
* hardware-id-shaped tokens (``hw27``), which name specific units.

Illustrate the add-on with **synthetic** data instead — the test suite
generates its own recording with the Rerun SDK, and that is the pattern to
follow for anything a reader is meant to see.

Run it on the whole repository (what CI does)::

    python3 tools/check_no_data.py

or on what is about to be committed (what the pre-commit hook does)::

    python3 tools/check_no_data.py --staged

Deliberate exceptions go in ``.no-data-allow`` as one repo-relative path per
line — they should be rare enough to argue about individually.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

# Recordings, point clouds, scenes, and anything rendered from them.
BLOCKED_SUFFIXES = {
    ".rrd", ".rbl",
    ".blend", ".blend1", ".blend2",
    ".ply", ".pcd", ".las", ".laz", ".e57", ".xyz", ".obj", ".glb", ".gltf",
    ".fbx", ".usd", ".usda", ".usdc", ".usdz", ".abc",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".exr",
    ".hdr", ".svg",
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".h264", ".h265",
    ".npy", ".npz", ".parquet", ".arrow", ".feather", ".pkl", ".pickle",
    ".csv", ".tsv", ".sqlite", ".db", ".bag", ".mcap", ".pcap", ".bin", ".dat",
}

# Big enough that it is data rather than source, whatever it claims to be.
MAX_BYTES = 256 * 1024

CONTENT_PATTERNS = (
    (
        re.compile(r"/(?:home|Users)/(?!<)[A-Za-z0-9._-]+/"),
        "an absolute path from someone's machine",
    ),
    (
        re.compile(r"\bhw\d{1,4}\b"),
        "a hardware-id-shaped token naming a specific unit",
    ),
)

# Only text worth scanning; the suffix rule already rejects everything binary.
SCANNED_SUFFIXES = {
    ".py", ".md", ".toml", ".txt", ".cfg", ".ini", ".yml", ".yaml", ".json",
    ".sh", ".bash", ".zsh", "", ".in", ".rst",
}

ALLOW_FILE = ".no-data-allow"


def _git(*args):
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return [line for line in result.stdout.splitlines() if line]


def _allowed():
    if not os.path.isfile(ALLOW_FILE):
        return set()
    with open(ALLOW_FILE, encoding="utf-8") as handle:
        return {
            line.strip()
            for line in handle
            if line.strip() and not line.startswith("#")
        }


def _paths(staged: bool):
    if staged:
        return _git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
    return _git("ls-files")


def _size_of(path: str, staged: bool) -> int:
    """Size of the version being committed, not of the working tree."""
    if staged:
        try:
            blob = subprocess.run(
                ["git", "cat-file", "-s", f":{path}"],
                capture_output=True, text=True, check=True,
            )
            return int(blob.stdout.strip())
        except (subprocess.CalledProcessError, ValueError):
            pass
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _content_of(path: str, staged: bool) -> str:
    if staged:
        blob = subprocess.run(
            ["git", "show", f":{path}"], capture_output=True, text=True, check=False
        )
        if blob.returncode == 0:
            return blob.stdout
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def check(staged: bool = False) -> list:
    allowed = _allowed()
    problems = []
    self_path = os.path.relpath(os.path.abspath(__file__), os.getcwd())

    for path in _paths(staged):
        if path in allowed:
            continue
        suffix = os.path.splitext(path)[1].lower()

        if suffix in BLOCKED_SUFFIXES:
            problems.append((path, f"'{suffix}' is a data or media file type"))
            continue

        size = _size_of(path, staged)
        if size > MAX_BYTES:
            problems.append(
                (path, f"{size:,} bytes — too big to be source ({MAX_BYTES:,} limit)")
            )
            continue

        # This file necessarily contains the patterns it looks for.
        if path == self_path:
            continue
        if suffix not in SCANNED_SUFFIXES:
            continue
        content = _content_of(path, staged)
        for pattern, description in CONTENT_PATTERNS:
            found = pattern.search(content)
            if found:
                problems.append((path, f"{description}: {found.group(0)!r}"))
                break

    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--staged", action="store_true",
        help="check what is about to be committed instead of the whole repository",
    )
    args = parser.parse_args(argv)

    problems = check(args.staged)
    scope = "staged for commit" if args.staged else "tracked in this repository"
    if not problems:
        print(f"no-data check: clean ({scope})")
        return 0

    print(f"no-data check FAILED — these must not be {scope}:", file=sys.stderr)
    for path, why in problems:
        print(f"  {path}\n      {why}", file=sys.stderr)
    print(
        "\nRecorded data and anything rendered from it does not belong in this\n"
        "repository — not even privately, because history keeps it and a\n"
        "force-push does not erase it. Use synthetic data, as the tests do.\n"
        f"A genuine exception can be listed in {ALLOW_FILE}.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
