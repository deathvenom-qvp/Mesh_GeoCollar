# Mesh_GeoCollar

<p align="center"><img src="docs/img/vibe-coded.svg" alt="VIBE CODED TO THE MAX" width="720"></p>

A geofence dog collar that runs on the [Meshtastic](https://meshtastic.org) LoRa mesh. Each collar
keeps its own fences, warns the dog near the line using an off-the-shelf training collar's beep,
vibrate and (optionally) shock, and, if the dog does get out, stops correcting and switches to
lost mode so the dog is easy to find over the mesh. A base station app, which runs in Docker or as
a Home Assistant app, manages fences and collars and shows where each dog is. A phone app for
Android and iOS connects straight to a collar over Bluetooth, to show where it is and mute its
lost alert once you have found the dog.

> **Status: early skeleton.** The fence logic, collar protocol, base app and phone app are written
> and tested on a PC. Nothing has run on real hardware yet. See [What is not done yet](#what-is-not-done-yet).

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

Beeps and vibrations can repeat every 2 to 10 seconds while the dog stays in a band.

No correction starts on a single GPS reading. When a fix first puts the dog in a band, the collar
waits 1 second, takes a new fix, and corrects only if the dog is still in a band. Each step up to
a stronger correction is checked the same way, so a shock takes two fixes in the danger band, at
least a second apart; if the two fixes are in different bands, the milder one counts.

| State | Collar does |
| --- | --- |
| Inside, clear of every band | Nothing; reports status every 10 minutes. |
| Warning (inside, in any band) | The band's correction, above, once a second fix has agreed. |
| Outside (not yet confirmed) | **Stops all corrections** at once. They stay off, even through the bands on the way back, until the dog is inside and clear of every band. |
| Lost (3 good fixes outside) | **Stops all corrections**, sends a breach alert, reports position every 30 s. |
| Outside for 90 s | **Lost beacon**: Bluetooth name "Rex is lost 5551234567" 90 s on / 90 s off, a plain-text "Please help get Rex home. Call …" on the public mesh channel every 5 minutes, GPS at half the lost rate. Until back inside or silenced from the base. |
| Back inside, clear of every band, for 3 fixes | Re-arms. |

Fixes with poor PDOP or few satellites are ignored entirely.

The collar also paces its own GPS: every 5 s near the line, slowing to once a minute deep inside
(the time a running dog would need to reach the outermost band), and every 15 s while lost. A
lost dog's collar that has had no answer from the base for 2 minutes (directly or through the
mesh) and has no phone app connected takes a fix only every 5 minutes, since nothing can pass
the position on; it goes back to the usual rate as soon as the base answers. Inside the fences
the base makes no difference: the collar enforces them on its own.

## Repository layout

| Path | What |
| --- | --- |
| `firmware/GeoCollar/` | The Meshtastic module (C++). Fence engine, CaiXianLin frame builder, wire protocol, Meshtastic glue. |
| `firmware/build.sh` | Checks out the latest Meshtastic patch release of the series in `firmware/MESHTASTIC_VERSION`, drops the module into `src/modules/optional/`, builds. |
| `firmware/test/` | PC tests for everything that does not need hardware (`make -C firmware/test`). |
| `base/` | Base station app (Python, FastAPI, Leaflet map), Dockerfile, and Home Assistant app manifest. |
| `app/` | Phone app for Android and iOS ([Capacitor](https://capacitorjs.com), plain JavaScript, Leaflet map). See [app/README.md](app/README.md). |
| `docs/protocol.md` | Message format between collars and base, and between a collar and the phone app. |
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
attaches the images. Each build of main is also published as the `firmware-latest` release
(packaged by `firmware/package.py`), which the base app downloads and flashes to collars over USB
from its **Collar firmware** panel. See [base/DOCS.md](base/DOCS.md#updating-collar-firmware).

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

The base radio is any Meshtastic node on stock firmware (2.5 or later) with its LoRa region set,
plugged into the Home Assistant machine by USB (recommended) or reachable on the network. See
[Setting up the base radio](base/DOCS.md#setting-up-the-base-radio).

Then, for each collar: plug it into the base machine, flash and set it up from **Collar
firmware** (that adds it, names it after the dog, and makes it trust the base), draw keep-in and
keep-out zones on the map, and send them.

The map starts on satellite imagery so you can trace the yard. Each collar holds up to 4 zones of
up to 32 corners. Draw one keep-in zone around the yard and keep-out zones inside it (a pond, a
flower bed): the collar requires the dog to be inside *every* keep-in zone, so two separate
keep-in zones would never both be satisfied. Each zone shows its danger, warning and attention
bands in their own colours, so you can see where each correction starts. Click a zone to drag its corners, then press **Send
to collar**; the page shows when the collar reports the new fence version. It warns before you
send fences that the dog's last position already breaks. The map libraries are bundled, so only
the map tiles need internet.

## The phone app

The base station needs the mesh to reach a collar. The phone app does not: it talks to the collar
directly over Bluetooth, from a few dozen metres away, wherever you are. It is meant for the walk
to fetch a lost dog.

1. In the base app, pick the collar and press **Connect app**. Name the phone, and a QR code appears.
2. In the phone app, tap **Add a collar** and scan the code. That is the phone's key to this one
   collar. The base hands the same key to the collar over the mesh.
3. Near the collar, the app connects by itself and shows the collar on a map next to your own
   position, with the distance and direction, its state, battery and GPS fix.
4. **Mute lost alert** stops the collar's lost beacon, the same as *Silence lost alert* on the
   base. The app can also make the collar beep or vibrate, and pause and resume its corrections.
   It can never shock.

Remove a phone under **Connect app** on the base and the collar stops accepting it. Up to four
phones can be linked to a collar. How to install the app, and what it does and does not protect
against, is in [app/README.md](app/README.md). There is a demo collar in the app to try it without
any hardware.

## Safety

- The phone app cannot shock, turn shock on, or change fences. The collar refuses those from a
  phone whatever the app sends.
- Shock is **off by default**. The owner turns it on per collar in the base app, after confirming.
  When enabled it is capped in level, count per breach and count per hour, and never fires on a poor GPS fix or in lost mode.
- Corrections stop as soon as the dog is confirmed outside: correcting a dog that is trying to come
  home teaches the wrong thing.
- Fit a hardware disable switch (GPIO 7 to ground) and test with the electrodes removed.
- Electronic shock collars are banned or restricted in some places. Check local law; beep and
  vibrate alone may be enough.

## What is not done yet

- Not yet run on hardware: the data-line tap, the RMT timing and pairing need bench testing.
- The lost beacon's Bluetooth advertising is written against Meshtastic's BLE stack and
  untested on hardware.
- GPS pacing is written and compiles but is untested on hardware, and its settings (5 s, 60 s,
  15 s, 6 m/s) can't be changed from the base app yet. On the Wireless Tracker it changes how
  often the collar takes a fix, not whether the GPS chip is powered (see `docs/hardware.md`).
- The firmware changes in 0.16.0 (fixes taken straight from the GPS, the "no GPS fix" report,
  numbered settings messages, the correction timing) pass the PC tests and compile, and are
  untested on hardware.
- The phone app has never met a real collar. Its messages are tested against the collar's own
  code built for a PC, the base's QR code has been scanned through the app's scanner in a desktop
  browser, and the screens have been tried there with the demo collar. The Bluetooth part
  (finding the collar, pairing with its PIN, Meshtastic's phone API over Bluetooth) and the
  collar's side of it (answering a phone, staying connectable during the lost beacon's quiet
  half) compile but are untested on hardware, on Android and on iOS. See
  [app/README.md](app/README.md#what-is-tested-and-what-is-not).
- The Android app is a debug build to install by hand, and the iOS app has to be built and signed
  with your own Apple developer account. Neither is in an app store.
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
