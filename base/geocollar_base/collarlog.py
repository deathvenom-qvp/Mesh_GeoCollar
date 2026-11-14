# SPDX-License-Identifier: GPL-3.0-or-later
"""Each collar's reports over time: when it reported, where it was, and how it was doing.

One file per collar in <data>/log, one JSON object per line. It is kept apart from geocollar.json,
which is rewritten whole on every change: a log grows with every report, so a line is appended
instead. A file keeps the newest KEEP reports; once it has grown a quarter past that, the oldest
are dropped.

An entry: {"t": seconds since 1970, "lat": ..., "lon": ..., "state": "INSIDE", "bat": percent,
"sats": satellites}. lat and lon are left out when the collar had no GPS fix at that moment.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

log = logging.getLogger(__name__)

KEEP = 20000


class CollarLog:
    def __init__(self, data_dir, keep: int = KEEP):
        self.root = Path(data_dir) / "log"
        self.keep = keep
        self._lock = threading.Lock()
        self._count: dict[str, int] = {}

    def _path(self, node_id: str) -> Path:
        if not re.fullmatch(r"![0-9a-f]{8}", node_id or ""):  # it becomes a file name
            raise ValueError("bad node ID")
        return self.root / f"{node_id[1:]}.jsonl"

    @staticmethod
    def _lines(path: Path) -> list[str]:
        try:
            return path.read_text().splitlines()
        except OSError:
            return []

    @staticmethod
    def _cut_short(path: Path) -> bool:
        """The last line was never finished: the power went while it was being written."""
        try:
            with open(path, "rb") as f:
                f.seek(-1, 2)
                return f.read(1) != b"\n"
        except OSError:  # no file yet, or an empty one
            return False

    def add(self, node_id: str, entry: dict) -> None:
        with self._lock:
            path = self._path(node_id)
            self.root.mkdir(parents=True, exist_ok=True)
            n = self._count.get(node_id)
            line = json.dumps(entry, separators=(",", ":")) + "\n"
            if n is None:  # the first report since the base started
                n = len(self._lines(path))
                if self._cut_short(path):
                    line = "\n" + line  # or this report would join the damaged line and be lost with it
            with open(path, "a") as f:
                f.write(line)
            n += 1
            if n > self.keep * 1.25:
                newest = self._lines(path)[-self.keep:]
                tmp = path.with_suffix(".tmp")
                tmp.write_text("\n".join(newest) + "\n")
                tmp.replace(path)
                n = len(newest)
            self._count[node_id] = n

    def read(self, node_id: str, since: float = 0, limit: int = 2000, after: float | None = None) -> dict:
        """The entries from `since` on, oldest first: at most `limit`, the newest ones. total is
        how many there are from `since` on. With `after`, only the entries later than that are
        returned (total still counts from `since`): what is new since the page last asked."""
        with self._lock:
            lines = self._lines(self._path(node_id))
        total, entries = 0, []
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue  # a line cut short when the power went
            if isinstance(e, dict) and isinstance(e.get("t"), (int, float)) and e["t"] >= since:
                total += 1
                if after is None or e["t"] > after:
                    entries.append(e)
        return {"total": total, "entries": entries[-limit:] if limit > 0 else []}

    def clear(self, node_id: str) -> None:
        with self._lock:
            self._path(node_id).unlink(missing_ok=True)
            self._count.pop(node_id, None)

    def move(self, old: str, new: str) -> None:
        """A collar's node ID changed (see Store.move_collar): its log goes with it."""
        with self._lock:
            src, dest = self._path(old), self._path(new)
            self._count.pop(old, None)
            self._count.pop(new, None)
            if src.exists():
                src.replace(dest)
