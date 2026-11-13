#!/usr/bin/env bash
# Builds Meshtastic firmware with the GeoCollar module for the Heltec Wireless Tracker.
#
# Meshtastic (https://github.com/meshtastic/firmware, GPL-3.0) is checked out at the version in
# MESHTASTIC_VERSION, and GeoCollar is dropped into src/modules/optional/, which Meshtastic's
# own build picks up automatically. No Meshtastic source file is modified.
#
# Usage: firmware/build.sh [platformio env]   (default: heltec-wireless-tracker)
# Needs: git, python3, PlatformIO (pip install platformio).
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

mkdir -p "$mt/src/modules/optional"
rm -rf "$mt/src/modules/optional/GeoCollar"
cp -r "$here/GeoCollar" "$mt/src/modules/optional/GeoCollar"

cd "$mt"
pio run -e "$env_name"

echo
echo "Firmware images are in $mt/.pio/build/$env_name/"
