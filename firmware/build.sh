#!/usr/bin/env bash
# Builds Meshtastic firmware with the GeoCollar module for the Heltec Wireless Tracker.
#
# Meshtastic (https://github.com/meshtastic/firmware, GPL-3.0) is checked out at the version in
# MESHTASTIC_VERSION, and GeoCollar is dropped into src/modules/optional/, which Meshtastic's
# own build picks up automatically. No Meshtastic source file is modified.
#
# Usage: firmware/build.sh [platformio env]   (default: heltec-wireless-tracker)
# Needs: git, python3, PlatformIO 6.2.0 (pip install platformio==6.2.0), the version Meshtastic pins.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
env_name="${1:-heltec-wireless-tracker}"
version="$(tr -d '[:space:]' < "$here/MESHTASTIC_VERSION")"
mt="$here/.meshtastic"

if [ ! -d "$mt/.git" ]; then
  git clone --depth 1 --branch "$version" --recurse-submodules --shallow-submodules \
    https://github.com/meshtastic/firmware.git "$mt"
else
  current="$(git -C "$mt" describe --tags --exact-match 2>/dev/null || true)"
  if [ "$current" != "$version" ]; then
    git -C "$mt" fetch --depth 1 origin tag "$version"
    git -C "$mt" checkout -q "$version"
    git -C "$mt" submodule update --init --depth 1
  fi
fi

# Work around pioarduino 6.2.0.post1: the ESP32 platform's second build stage runs PlatformIO from
# its own Python env ("penv"), and that post-release reports itself as 6.2.0-post.1, which the
# platform's ">=6.2.0" check rejects. Pre-seed the penv with pioarduino 6.2.0 so it is kept.
pio_python="$(head -1 "$(command -v pio)" | sed 's/^#!//')"
penv="${PLATFORMIO_CORE_DIR:-$HOME/.platformio}/penv"
if [ ! -x "$penv/bin/python" ]; then
  "$pio_python" -m venv "$penv"
fi
if ! "$penv/bin/python" -c "import importlib.metadata as m, sys; sys.exit(m.version('pioarduino') != '6.2.0')" 2>/dev/null; then
  if [ -x "$penv/bin/uv" ]; then # a penv made by the platform has uv but no pip
    "$penv/bin/uv" pip install -q --python "$penv/bin/python" "pioarduino==6.2.0"
  else
    "$penv/bin/python" -m pip install -q "pioarduino==6.2.0"
  fi
fi

mkdir -p "$mt/src/modules/optional"
rm -rf "$mt/src/modules/optional/GeoCollar"
cp -r "$here/GeoCollar" "$mt/src/modules/optional/GeoCollar"

cd "$mt"
pio run -e "$env_name"

echo
echo "Firmware images are in $mt/.pio/build/$env_name/"
