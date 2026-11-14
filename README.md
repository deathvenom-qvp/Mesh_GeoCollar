# Mesh_GeoCollar

<p align="center"><img src="docs/img/vibe-coded.svg" alt="VIBE CODED TO THE MAX" width="720"></p>

A geofence dog collar that runs on the [Meshtastic](https://meshtastic.org) LoRa mesh. Each collar
keeps its own fences, warns the dog near the line using an off-the-shelf training collar's beep,
vibrate and (optionally) shock, and, if the dog does get out, stops correcting and switches to
lost mode so the dog is easy to find over the mesh. A base station app, which runs in Docker or as
a Home Assistant app, manages fences and collars and shows where each dog is.

> **Status: early skeleton.** The fence logic, collar protocol and base app are written and tested
> on a PC. Nothing has run on real hardware yet. See [What is not done yet](#what-is-not-done-yet).

**Built on Meshtastic. Collar protocol by OpenShock.** See [Credits](#credits).

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

The line you draw is the outer edge of three bands inside it, each with its own width:

| Band (from the line inwards) | Collar does |
| --- | --- |
| Attention (innermost) | Beep. |
| Warning | Vibrate. |
| Danger (next to the line, optional) | Shock, only if shock is enabled on the collar, capped per breach and per hour; otherwise vibrate. Turn the danger band off and the zone never shocks. |

| State | Collar does |
| --- | --- |
| Inside, clear of every band | Nothing; reports status every 10 minutes. |
| Warning (inside, in any band) | The band's correction, above. |
| Outside (not yet confirmed) | Keeps correcting: shock only if the zone has a danger band and shock is enabled; otherwise vibrate. |
| Lost (3 good fixes outside) | **Stops all corrections**, sends a breach alert, reports position every 30 s. |
| Back inside by 5 m for 3 fixes | Re-arms. |

Fixes with poor PDOP or few satellites are ignored entirely.

The collar also paces its own GPS: every 5 s near the line, slowing to once a minute deep inside
(the time a running dog would need to reach the outermost band), and every 15 s while lost.

## Repository layout

| Path | What |
| --- | --- |
| `firmware/GeoCollar/` | The Meshtastic module (C++). Fence engine, CaiXianLin frame builder, wire protocol, Meshtastic glue. |
| `firmware/build.sh` | Checks out the latest Meshtastic patch release of the series in `firmware/MESHTASTIC_VERSION`, drops the module into `src/modules/optional/`, builds. |
| `firmware/test/` | PC tests for everything that does not need hardware (`make -C firmware/test`). |
| `base/` | Base station app (Python, FastAPI, Leaflet map), Dockerfile, and Home Assistant app manifest. |
| `docs/protocol.md` | Message format between collars and base. |
| `docs/hardware.md` | Wiring the Tracker into the training collar, power, antennas, Meshtastic settings. |
| `reference/` | Meshtastic and OpenShock firmware as git submodules, for reading only. Not used by the build. |

## Build the collar firmware

```sh
git clone https://github.com/deathvenom-qvp/Mesh_GeoCollar && cd Mesh_GeoCollar
git submodule update --init --checkout --depth 1 reference/meshtastic-firmware reference/openshock-firmware   # optional
pip install platformio==6.2.0   # the version Meshtastic 2.8 pins
firmware/build.sh            # Heltec Wireless Tracker V1.1
```

GeoCollar uses Meshtastic's optional-module slot, so no Meshtastic file is modified. Each build
uses the newest patch release of the Meshtastic series named in `firmware/MESHTASTIC_VERSION`
(currently `2.8`), preferring a stable release and otherwise the newest alpha. GeoCollar needs 2.8
or later: older series lack the optional-module slot and admin-key checks it uses. Moving to a new series is a one-line change. To
build against one exact release, run `MESHTASTIC_VERSION=v2.8.2.d5f1619 firmware/build.sh`. Pin and ID settings
are in `firmware/GeoCollar/GeoCollarConfig.h`. CI builds the firmware on every pull request and
attaches the images.

## Run the base station

### Docker

```sh
docker compose up -d        # edit docker-compose.yml for your node's USB port or IP first
# open http://localhost:8099
```

### Home Assistant

[![Add repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fdeathvenom-qvp%2FMesh_GeoCollar)

1. Click the button above, or go to **Settings > Add-ons** (called **Apps** in newer releases) >
   **Add-on Store** > **⋮** > **Repositories** and add `https://github.com/deathvenom-qvp/Mesh_GeoCollar`.
2. Install **GeoCollar base**. Home Assistant builds it on your machine, which takes a few
   minutes the first time (longer on a Raspberry Pi).
3. On the **Configuration** tab, pick the Meshtastic node's USB device, or enter its IP address
   if it is on the network. Start the app and turn on **Show in sidebar**.

Home Assistant OS or Supervised is needed for apps (amd64 or aarch64). Full instructions,
including pairing collars, are in [base/DOCS.md](base/DOCS.md), which Home Assistant also shows
on the app's Documentation tab.

Then, for each collar: put the base node's public key in the collar's Meshtastic admin keys, add
the collar by node ID in the app, draw keep-in and keep-out zones on the map, and send them.

The map starts on satellite imagery so you can trace the yard. Each collar holds up to 4 zones of
up to 32 corners. Draw one keep-in zone around the yard and keep-out zones inside it (a pond, a
flower bed): the collar requires the dog to be inside *every* keep-in zone, so two separate
keep-in zones would never both be satisfied. Each zone shows its danger, warning and attention
bands in their own colours, so you can see where each correction starts. Click a zone to drag its corners, then press **Send
to collar**; the page shows when the collar reports the new fence version. It warns before you
send fences that the dog's last position already breaks. The map libraries are bundled, so only
the map tiles need internet.

## Safety

- Shock is **off by default**. When enabled it is capped in level, count per breach and count per
  hour, and never fires on a poor GPS fix or in lost mode.
- Corrections stop as soon as the dog is confirmed outside: correcting a dog that is trying to come
  home teaches the wrong thing.
- Fit a hardware disable switch (GPIO 7 to ground) and test with the electrodes removed.
- Electronic shock collars are banned or restricted in some places. Check local law; beep and
  vibrate alone may be enough.

## What is not done yet

- Not yet run on hardware: the data-line tap, the RMT timing and pairing need bench testing.
- GPS pacing is written and compiles but is untested on hardware, and its settings (5 s, 60 s,
  15 s, 6 m/s) can't be changed from the base app yet.
- Home Assistant entities (device trackers, breach notifications) are not wired up yet; the app
  has its own map for now. The official Meshtastic Home Assistant integration can show nodes
  meanwhile.
- No authentication on the base app's web page: run it on a trusted network or behind Home
  Assistant ingress.

## Credits

This project would not exist without two open-source firmwares. Both are included under
[`reference/`](reference) as git submodules, with their own licences, so their code can be read
next to ours. They are skipped by `git clone --recursive` (Home Assistant clones the repository
that way, and OpenShock has a submodule only reachable over SSH); fetch them with:

```sh
git submodule update --init --checkout --depth 1 reference/meshtastic-firmware reference/openshock-firmware
```

- **[Meshtastic firmware](https://github.com/meshtastic/firmware)** (GPL-3.0), by the Meshtastic
  project and its contributors. The collar firmware *is* Meshtastic: GeoCollar is an optional
  module built into an unmodified Meshtastic release, which provides the LoRa mesh, GPS,
  encryption, remote admin and firmware updates. The base app uses the
  [Meshtastic Python library](https://github.com/meshtastic/python). Meshtastic® is a registered
  trademark of Meshtastic LLC; this project is not affiliated with or endorsed by it.
  Reference copy: `reference/meshtastic-firmware`, pinned to the release we build against.
- **[OpenShock firmware](https://github.com/OpenShock/Firmware)** (AGPL-3.0), by the OpenShock
  project and its contributors. OpenShock reverse engineered and documented the CaiXianLin
  collar's 433 MHz protocol ([wiki](https://wiki.openshock.org/hardware/shockers/caixianlin)).
  Our frame builder is an independent implementation of that protocol, checked against
  OpenShock's encoder; no OpenShock code is copied. Reference copy: `reference/openshock-firmware`.

Other libraries and data are listed in [CREDITS.md](CREDITS.md).

## Licence

GPL-3.0-or-later, the same licence as Meshtastic's firmware. If you distribute collars running
this firmware, you must also offer the source. See [LICENSE](LICENSE) and [CREDITS.md](CREDITS.md).
