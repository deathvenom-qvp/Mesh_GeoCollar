# Mesh_GeoCollar

A geofence dog collar that runs on the [Meshtastic](https://meshtastic.org) LoRa mesh. Each collar
keeps its own fences, warns the dog near the line using an off-the-shelf training collar's beep,
vibrate and (optionally) shock, and, if the dog does get out, stops correcting and switches to
lost mode so the dog is easy to find over the mesh. A base station app, which runs in Docker or as
a Home Assistant app, manages fences and collars and shows where each dog is.

> **Status: early skeleton.** The fence logic, collar protocol and base app are written and tested
> on a PC. Nothing has run on real hardware yet. See [What is not done yet](#what-is-not-done-yet).

**Built on Meshtastic. Collar protocol by OpenShock.** See [CREDITS.md](CREDITS.md).

## How it works

```
 Collar (Heltec Wireless Tracker)                     Base station
 ┌────────────────────────────────────┐   LoRa mesh   ┌───────────────────────┐   USB/TCP   ┌────────────────────────┐
 │ Meshtastic + GeoCollar module      │ ◄───────────► │ Meshtastic node       │ ◄─────────► │ GeoCollar base app     │
 │  - fences saved on the collar      │  encrypted DMs│ (private channel,     │             │ (Docker or Home        │
 │  - checks every GPS fix            │  other nodes  │  collars' admin key)  │             │  Assistant app): map,  │
 │  - beep / vibrate / shock via GPIO │  relay but    └───────────────────────┘             │  fences, collars       │
 │    into the training collar board  │  can't read                                         └────────────────────────┘
 └────────────────────────────────────┘
```

| State | Collar does |
| --- | --- |
| Inside | Nothing; reports status every 10 minutes. |
| Warning (inside, within the zone's margin) | Beep, then vibrate when under half the margin. |
| Outside (not yet confirmed) | Vibrate; shock only if the owner enabled it, capped per breach and per hour. |
| Lost (3 good fixes outside) | **Stops all corrections**, sends a breach alert, reports position every 30 s. |
| Back inside by 5 m for 3 fixes | Re-arms. |

Fixes with poor PDOP or few satellites are ignored entirely.

## Repository layout

| Path | What |
| --- | --- |
| `firmware/GeoCollar/` | The Meshtastic module (C++). Fence engine, CaiXianLin frame builder, wire protocol, Meshtastic glue. |
| `firmware/build.sh` | Checks out Meshtastic at `firmware/MESHTASTIC_VERSION`, drops the module into `src/modules/optional/`, builds. |
| `firmware/test/` | PC tests for everything that does not need hardware (`make -C firmware/test`). |
| `base/` | Base station app (Python, FastAPI, Leaflet map), Dockerfile, and Home Assistant app manifest. |
| `docs/protocol.md` | Message format between collars and base. |
| `docs/hardware.md` | Wiring the Tracker into the training collar, power, antennas, Meshtastic settings. |

## Build the collar firmware

```sh
pip install platformio==6.2.0   # the version Meshtastic v2.8.2 pins
firmware/build.sh            # Heltec Wireless Tracker V1.1
```

GeoCollar uses Meshtastic's optional-module slot, so no Meshtastic file is modified and moving to a
new Meshtastic release is a one-line change to `firmware/MESHTASTIC_VERSION`. Pin and ID settings
are in `firmware/GeoCollar/GeoCollarConfig.h`. CI builds the firmware on every pull request and
attaches the images.

## Run the base station

Docker:

```sh
docker compose up -d        # edit docker-compose.yml for your node's USB port or IP first
# open http://localhost:8099
```

Home Assistant: add this repository under Settings > Add-ons (called Apps in newer releases) >
store > Repositories, install **GeoCollar base**, and pick the Meshtastic node's serial port or IP
in its options.

Then, for each collar: put the base node's public key in the collar's Meshtastic admin keys, add
the collar by node ID in the app, draw keep-in and keep-out zones on the map, and send them.

## Safety

- Shock is **off by default**. When enabled it is capped in level, count per breach and count per
  hour, and never fires on a poor GPS fix or in lost mode.
- Corrections stop as soon as the dog is confirmed outside: correcting a dog that is trying to come
  home teaches the wrong thing.
- Fit a hardware disable switch (`GEOCOLLAR_DISABLE_PIN`) and test with the electrodes removed.
- Electronic shock collars are banned or restricted in some places. Check local law; beep and
  vibrate alone may be enough.

## What is not done yet

- Not yet run on hardware: the data-line tap, the RMT timing and pairing need bench testing.
- GPS pacing (faster fixes near the line, slower deep inside) is planned but not implemented; set
  the Meshtastic GPS update interval by hand for now.
- Home Assistant entities (device trackers, breach notifications) are not wired up yet; the app
  has its own map for now. The official Meshtastic Home Assistant integration can show nodes
  meanwhile.
- No authentication on the base app's web page: run it on a trusted network or behind Home
  Assistant ingress.

## Licence

GPL-3.0-or-later, the same licence as Meshtastic's firmware. If you distribute collars running
this firmware, you must also offer the source. See [LICENSE](LICENSE) and [CREDITS.md](CREDITS.md).
