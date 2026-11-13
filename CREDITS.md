# Credits

Mesh_GeoCollar stands on the work of two open-source projects. Thank you to both communities.

## Meshtastic

[Meshtastic](https://meshtastic.org) ([firmware source](https://github.com/meshtastic/firmware)) is the
off-grid LoRa mesh this project runs on. The collar firmware *is* Meshtastic: GeoCollar is an
optional module dropped into an unmodified Meshtastic release (see `firmware/MESHTASTIC_VERSION`),
and it relies on Meshtastic for the mesh, GPS, encryption (including PKI direct messages), remote
administration and Wi-Fi/Bluetooth firmware updates. The base station uses the official
[Meshtastic Python library](https://github.com/meshtastic/python).

- Meshtastic firmware is licensed under the GNU General Public License v3.0.
- A copy is in `reference/meshtastic-firmware` (git submodule, pinned to the release in
  `firmware/MESHTASTIC_VERSION`) for reading. Its `LICENSE` file is unchanged.
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

- [Leaflet](https://leafletjs.com) and [Leaflet.draw](https://github.com/Leaflet/Leaflet.draw) for
  the fence map (BSD-2-Clause / MIT), with map tiles from
  [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors.
- [FastAPI](https://fastapi.tiangolo.com) and [Uvicorn](https://www.uvicorn.org) for the base app.
