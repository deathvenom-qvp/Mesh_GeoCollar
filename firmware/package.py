#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Packages a GeoCollar firmware build for the base app to flash.

Copies the images out of PlatformIO's build folder under fixed names, adds Meshtastic's Wi-Fi OTA
loader (https://github.com/meshtastic/esp32-unified-ota, GPL-3.0), and writes manifest.json with
each file's SHA-256 and the flash offsets read from the build's own partition table.

Usage: firmware/package.py <out dir> [platformio env]    (run after firmware/build.sh)
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import struct
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The OTA loader that Meshtastic 2.7.18+ reboots into for Wi-Fi updates. It lives in the app1
# partition, so it is flashed once over USB and then stays put across Wi-Fi updates.
LOADER_VERSION = "v1.0.1"
LOADER_URL = "https://github.com/meshtastic/esp32-unified-ota/releases/download/{version}/mt-{mcu}-ota.bin"
MCU = {"heltec-wireless-tracker": "esp32s3", "heltec-wireless-tracker-V1-0": "esp32s3"}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_partitions(path: Path) -> dict[str, dict]:
    """ESP-IDF binary partition table: 32-byte entries starting with magic 0xAA50."""
    parts = {}
    data = path.read_bytes()
    for i in range(0, len(data) - 31, 32):
        magic, ptype, subtype, offset, size, label, _flags = struct.unpack_from("<HBBII16sI", data, i)
        if magic != 0x50AA:
            break
        name = label.rstrip(b"\0").decode("ascii", "replace")
        parts[name] = {"type": ptype, "subtype": subtype, "offset": offset, "size": size}
    return parts


def by_subtype(parts: dict, ptype: int, subtype: int) -> dict:
    for p in parts.values():
        if p["type"] == ptype and p["subtype"] == subtype:
            return p
    raise SystemExit(f"partition type {ptype} subtype {subtype:#x} not found")


def git(*args: str, cwd: Path) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=cwd, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("env", nargs="?", default="heltec-wireless-tracker")
    ap.add_argument("--loader", type=Path, help="use this OTA loader file instead of downloading it")
    args = ap.parse_args()

    mt = HERE / ".meshtastic"
    build = mt / ".pio" / "build" / args.env
    factories = sorted(build.glob("firmware-*.factory.bin"))
    if not factories:
        sys.exit(f"no firmware-*.factory.bin in {build}; run firmware/build.sh {args.env} first")
    factory = factories[-1]
    app = factory.with_name(factory.name.replace(".factory.bin", ".bin"))
    parts = read_partitions(build / "partitions.bin")
    app0 = by_subtype(parts, 0, 0x10)  # app, ota_0: the Meshtastic firmware
    app1 = by_subtype(parts, 0, 0x11)  # app, ota_1: the OTA loader
    otadata = by_subtype(parts, 1, 0x00)  # data, ota: which app slot boots
    if app.stat().st_size > app0["size"]:
        sys.exit(f"{app.name} is larger than the app partition")

    mcu = MCU.get(args.env, "esp32s3")
    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    names = {
        "app": f"geocollar-{args.env}.bin",
        "factory": f"geocollar-{args.env}.factory.bin",
        "loader": f"mt-{mcu}-ota.bin",
    }
    shutil.copyfile(app, out / names["app"])
    shutil.copyfile(factory, out / names["factory"])
    if args.loader:
        shutil.copyfile(args.loader, out / names["loader"])
    else:
        url = LOADER_URL.format(version=LOADER_VERSION, mcu=mcu)
        with urllib.request.urlopen(url, timeout=60) as r:
            (out / names["loader"]).write_bytes(r.read())
    if (out / names["loader"]).stat().st_size > app1["size"]:
        sys.exit("the OTA loader is larger than its partition")

    repo = HERE.parent
    manifest = {
        "format": 1,
        "board": args.env,
        "mcu": mcu,
        "geocollar_commit": git("rev-parse", "HEAD", cwd=repo),
        "geocollar_subject": git("log", "-1", "--format=%s", cwd=repo),
        "meshtastic_version": git("describe", "--tags", "--exact-match", cwd=mt) or git("rev-parse", "--short", "HEAD", cwd=mt),
        "loader_version": LOADER_VERSION,
        "built_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "offsets": {
            "factory": 0,
            "app": app0["offset"],
            "loader": app1["offset"],
            "otadata": otadata["offset"],
            "otadata_size": otadata["size"],
        },
        "files": {
            k: {"name": n, "size": (out / n).stat().st_size, "sha256": sha256(out / n)} for k, n in names.items()
        },
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
