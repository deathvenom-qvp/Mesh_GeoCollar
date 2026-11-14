# Credits

Mesh_GeoCollar stands on the work of two open-source projects. Thank you to both communities.

## Meshtastic

[Meshtastic](https://meshtastic.org) ([firmware source](https://github.com/meshtastic/firmware)) is the
off-grid LoRa mesh this project runs on. The collar firmware *is* Meshtastic: GeoCollar is an
optional module dropped into an unmodified Meshtastic release (the latest patch of the series in
`firmware/MESHTASTIC_VERSION`),
and it relies on Meshtastic for the mesh, GPS, encryption (including PKI direct messages), remote
administration and Wi-Fi/Bluetooth firmware updates. The base station uses the official
[Meshtastic Python library](https://github.com/meshtastic/python).

- Meshtastic firmware is licensed under the GNU General Public License v3.0.
- A copy is in `reference/meshtastic-firmware` (git submodule, pinned to the release
  the module was last checked against) for reading. Its `LICENSE` file is unchanged.
- Meshtastic® is a registered trademark of Meshtastic LLC. This project is not affiliated with
  or endorsed by Meshtastic LLC.

## OpenShock

[OpenShock](https://openshock.org) ([firmware source](https://github.com/OpenShock/Firmware))
reverse engineered and documented the 433 MHz protocol of CaiXianLin-type training collars
([protocol notes](https://wiki.openshock.org/hardware/shockers/caixianlin)). GeoCollar's
`firmware/GeoCollar/CaiXianLin.*` is an independent implementation written from that
documentation and checked against the frame layout of OpenShock's `CaiXianlinEncoder`
(sync pulse, 16-bit ID, 4-bit channel, 4-bit mode, 8-bit intensity, Sum8 checksum, 3-bit
postamble, and the "vibrate at 0" stop frame). No OpenShock source code is copied.

- OpenShock firmware is licensed under the GNU Affero General Public License v3.0.
- A copy is in `reference/openshock-firmware` (git submodule) for reading only. It is not built
  or linked into GeoCollar. Its `LICENSE` file is unchanged.

## Also used

- [Leaflet](https://leafletjs.com) 1.9.4 (BSD-2-Clause) and
  [Leaflet-Geoman](https://github.com/geoman-io/leaflet-geoman) 2.17.0 (MIT) for the fence map,
  and [polygon-clipping](https://github.com/mfogel/polygon-clipping) 0.15.7 (MIT) for drawing the
  correction bands, vendored with their licences in `base/geocollar_base/static/vendor/`. Map tiles from
  [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors and satellite imagery
  from Esri World Imagery.
- [@tmcw/togeojson](https://github.com/placemark/togeojson) 7.1.3 (BSD-2-Clause) and
  [shpjs](https://github.com/calvinmetcalf/shapefile-js) 6.2.0 (MIT, bundling proj4js) for
  importing property lines from KML and shapefiles, vendored with their licences.
- [esptool](https://github.com/espressif/esptool) (GPL-2.0-or-later) for flashing collars over USB
  from the base, and [esptool-js](https://github.com/espressif/esptool-js) 0.5.7 (Apache-2.0,
  bundling pako, MIT and Zlib) for flashing from the browser, vendored with its licence in
  `base/geocollar_base/static/vendor/esptool-js/`.
- Meshtastic's [ESP32 unified OTA loader](https://github.com/meshtastic/esp32-unified-ota)
  v1.0.1 (GPL-3.0) for Wi-Fi updates. `firmware/package.py` downloads its release image and ships
  it unchanged next to the GeoCollar firmware.
- [FastAPI](https://fastapi.tiangolo.com) and [Uvicorn](https://www.uvicorn.org) for the base app.
