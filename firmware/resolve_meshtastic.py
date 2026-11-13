#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Print the Meshtastic firmware tag to build against.

Usage: resolve_meshtastic.py SPEC

SPEC is either a full tag such as v2.8.2.d5f1619, which is used as is, or a release series such as
2.8, which resolves to the newest Meshtastic release in that series, i.e. the latest patch. A stable
release wins when the series has one; otherwise the newest pre-release (alpha) is used, with a
warning. Drafts are never used. Releases come from the GitHub API (set GITHUB_TOKEN to avoid its
rate limit). If the API can't be reached, the newest tag in the series is found with git instead;
that fallback can't tell alpha from beta releases.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

REPO = "meshtastic/firmware"
GIT_URL = f"https://github.com/{REPO}.git"
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)\.([0-9a-f]+)$")


def in_series(tag, series):
    m = TAG_RE.match(tag)
    return bool(m) and f"{m.group(1)}.{m.group(2)}" == series


def from_api(series):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "geocollar-build"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    best = None
    best_pre = None
    for page in range(1, 4):
        req = urllib.request.Request(
            f"https://api.github.com/repos/{REPO}/releases?per_page=100&page={page}", headers=headers
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            releases = json.load(r)
        if not releases:
            break
        for rel in releases:
            if rel["draft"] or not in_series(rel["tag_name"], series):
                continue
            if rel["prerelease"]:
                if best_pre is None or rel["published_at"] > best_pre["published_at"]:
                    best_pre = rel
            elif best is None or rel["published_at"] > best["published_at"]:
                best = rel
    if best:
        return best["tag_name"]
    if best_pre:
        print(f"resolve_meshtastic: {series} has no stable release yet; using pre-release "
              f"{best_pre['tag_name']}", file=sys.stderr)
        return best_pre["tag_name"]
    return None


def from_git(series):
    out = subprocess.run(
        ["git", "ls-remote", "--tags", "--refs", GIT_URL, f"v{series}.*"],
        check=True, capture_output=True, text=True,
    ).stdout
    tags = [line.split("refs/tags/")[1] for line in out.splitlines() if "refs/tags/" in line]
    tags = [t for t in tags if in_series(t, series)]
    if not tags:
        return None
    top = max(int(TAG_RE.match(t).group(3)) for t in tags)
    tags = [t for t in tags if int(TAG_RE.match(t).group(3)) == top]
    if len(tags) == 1:
        return tags[0]
    # Several builds of the same patch number: take the newest commit.
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["git", "init", "-q", d], check=True)
        subprocess.run(
            ["git", "-C", d, "fetch", "-q", "--depth", "1", GIT_URL] + [f"refs/tags/{t}:refs/tags/{t}" for t in tags],
            check=True,
        )
        dated = []
        for t in tags:
            ts = subprocess.run(
                ["git", "-C", d, "log", "-1", "--format=%ct", t], check=True, capture_output=True, text=True
            ).stdout.strip()
            dated.append((int(ts), t))
    return max(dated)[1]


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    spec = sys.argv[1].strip()
    if TAG_RE.match(spec):
        print(spec)
        return
    if not re.fullmatch(r"\d+\.\d+", spec):
        sys.exit(f"resolve_meshtastic: '{spec}' is neither a tag (v2.8.2.d5f1619) nor a series (2.8)")
    try:
        tag = from_api(spec)
    except Exception as e:  # network, rate limit
        print(f"resolve_meshtastic: GitHub API failed ({e}); falling back to git tags", file=sys.stderr)
        tag = from_git(spec)
    if not tag:
        sys.exit(f"resolve_meshtastic: no Meshtastic release found in series {spec}")
    print(tag)


if __name__ == "__main__":
    main()
