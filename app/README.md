# GeoCollar phone app

An app for Android and iOS that talks to a GeoCollar collar directly over Bluetooth. The base
station needs the mesh to reach a collar; the phone needs only to be within Bluetooth range, a
few dozen metres. It is for the walk to fetch a dog that got out:

- the collar on a map next to your own position, with the distance and the direction;
- its state (inside, near the line, outside, lost), battery and GPS fix;
- **Mute lost alert**, which stops the collar's lost beacon until the dog has been home again;
- **Beep** and **Vibrate**, and pausing and resuming the fence's corrections.

It cannot shock, turn shock on, or change fences or settings. The collar refuses those from a
phone, whatever a phone sends.

> **Not yet tried on a real collar or a real phone.** See
> [What is tested and what is not](#what-is-tested-and-what-is-not).

## Install

### Android

1. On the phone, open the repository's
   [app-latest release](https://github.com/deathvenom-qvp/Mesh_GeoCollar/releases/tag/app-latest)
   and download `geocollar-app.apk`. (It appears there once a build of `main` with the app has
   run. Before that, each pull request's **Phone app build (Android)** check has the same file
   under its artifacts.)
2. Open the file. Android asks whether to allow installing apps from your browser or file
   manager: allow it, then install.
3. Open GeoCollar and allow Bluetooth ("Nearby devices"), the camera, and location when it asks.

This is a debug build, signed with a throwaway key that changes with every build. To install a
newer one, uninstall the old one first, which also forgets the linked collars: link them again
from the base station. A properly signed release is on the list of [things not done](#not-done-yet).

### iPhone and iPad

Apple only lets a signed app onto a device, so there is nothing to download. You need a Mac with
Xcode and an Apple ID (a free one works; the app then needs reinstalling every 7 days, a paid
developer account lifts that):

```sh
cd app
npm ci
npm run ios          # builds the web part, syncs it, and opens Xcode
```

In Xcode, select the **App** target, pick your team under **Signing & Capabilities**, change the
bundle identifier to one of your own, plug in the phone and press Run.

## Link a collar

1. In the base station, select the collar and press **Connect app**. Say whose phone it is.
2. In the app, tap **Add a collar** and point the camera at the QR code. Or scan it with the
   phone's own camera app, which opens GeoCollar. If no camera can be used, press **Copy the code
   as text** on the base and paste it under *Type or paste the code instead*.
3. Go near the collar. The first time, the phone asks for the collar's Bluetooth PIN; the app
   shows it if the base knows it.

The base hands the phone's key to the collar over the mesh, with the collar's next report. Until
then the app says the collar has not been told about this phone yet, and keeps trying.

**Try a demo collar** on the first screen shows the app with a pretend lost dog near you, with no
hardware at all. One of the base station's simulated collars gets the same pretend dog, because a
simulated collar has no Bluetooth to connect to.

## How it works

```
 Base station                         Collar                              Phone
 "Connect app" makes a key  ──mesh──► keeps the key in flash
          │                           (AppKeys, PKI encrypted)
          └── QR code ───────────────────────────────────────────────►  keeps the key in the
                                                                         app's private storage
                                      Meshtastic's Bluetooth service ◄── pairs with the PIN,
                                      GeoCollar module               ◄── signs every request
                                                                         with the key
```

- The app uses the collar's own Meshtastic Bluetooth service, the one the Meshtastic app uses,
  and Meshtastic's phone API over it. GeoCollar messages travel in packets addressed to the
  collar itself on port 300, and never go on the air.
- Bluetooth pairing (the PIN) gets a phone to that service. It does not make the collar obey.
  The GeoCollar module on the collar answers only requests signed with a key the base gave it:
  HMAC-SHA256, a new session for every connection with fresh random numbers from both sides, and
  a counter in every request, so a recording is useless.
- The key is 16 random bytes, different for every phone and every collar. The base shows it
  once, in the QR code. Removing the phone on the base removes the key from the collar.
- The app finds the collar by its Bluetooth name: the Meshtastic name (`REX_c3d4`) or, while the
  dog is lost, the beacon's name (`Rex is lost 5551234567`). After connecting it checks the
  collar's node number, and the collar checks the app's key.
- Map tiles come from OpenStreetMap and Esri and need internet. Without it the map is blank, and
  the distance, the direction and **Open in maps** still work.

The message layouts are in [docs/protocol.md](../docs/protocol.md#the-companion-app).

### What the key does and does not protect

- Someone with only the collar's Bluetooth PIN can use Meshtastic's own app on the collar, as
  before, but cannot use GeoCollar's phone commands.
- Someone who photographs the QR code can link their own phone. They would still need the
  Bluetooth PIN and to be within Bluetooth range, and could then see the collar's position, mute
  the lost alert, beep, vibrate, and pause corrections. Treat the code like a house key; if in
  doubt, remove the phone on the base and make a new code.
- The key sits in the app's private storage on the phone and is left out of Android's cloud
  backup. Anyone who can unlock the phone can use the app.
- Messages between the app and the collar are signed, not encrypted on their own: the Bluetooth
  link's encryption is what hides the position from eavesdroppers.

## Build it

Needs Node.js 22 or later. For Android, Android Studio or the Android SDK with JDK 21. For iOS, a
Mac with Xcode 16 or later.

```sh
cd app
npm ci
npm test             # builds firmware/test/applink_collar first if it is missing (needs make and g++)
npm run build        # the web part, into www/
npm run android      # build, sync, and open Android Studio
npm run ios          # build, sync, and open Xcode
```

To try the screens in a desktop browser, run `npm run build` and serve `www/` with any static
web server (`python3 -m http.server -d www`). Bluetooth works only in the phone app, so in a
browser only the demo collar connects.

| Path | What |
| --- | --- |
| `src/applink.js` | The app's half of the signed session with the collar. The collar's half is `firmware/GeoCollar/AppLink.cpp`. |
| `src/meshtastic.js`, `src/radio.js` | Just enough of Meshtastic's phone API: the few protobuf fields needed, written and read by hand. |
| `src/ble.js`, `src/match.js` | Bluetooth: finding the collar, pairing, reading and writing. |
| `src/collar.js` | Keeps one collar connected: connect, prove the key, poll the status, carry commands, reconnect. |
| `src/pairing.js` | Reads the base station's pairing code. |
| `src/demo.js`, `src/fakenode.js` | The demo collar: the collar's half of the protocol in JavaScript behind a pretend Meshtastic node. |
| `src/main.js`, `src/index.html`, `src/style.css` | The screens. |
| `test/` | `node --test`. The link is tested against the demo collar and against the firmware's real session code. |
| `android/`, `ios/` | The native projects Capacitor wraps the web part in: permissions, icons, the `geocollar://` link. |
| `tools/make_icons.py` | Draws the icon and launch screen into both projects. |

## What is tested and what is not

Tested, on a PC:

- The app's messages against the collar's: the same golden bytes in the C++, Python and
  JavaScript tests, and the app's whole connection logic run against the collar's real session
  code (`firmware/GeoCollar/AppLink.cpp`, built for the PC as `firmware/test/applink_collar`):
  linking, status, muting the lost alert, beep, vibrate, corrections, a refused shock, a removed
  phone, a collar that restarts.
- The Meshtastic messages the app writes and reads, against bytes made with Meshtastic's own
  Python library.
- The base station's real QR code, scanned by the app's scanner through a pretend camera in a
  desktop browser.
- The screens, in a desktop browser at phone size, with the demo collar.
- That the Android and iOS projects compile (on GitHub, with every pull request).

Not tested, because it needs a collar and a phone:

- Everything Bluetooth: finding the collar by name, pairing with its PIN on Android and on iOS,
  and Meshtastic's phone API over a real connection. `src/ble.js` has never run.
- The collar's side on hardware: answering a phone through Meshtastic's phone API, and staying
  connectable in the quiet half of the lost beacon.
- The camera, location and `geocollar://` links on a real phone.

## Not done yet

- A signed Android release (so updates install over each other), and store listings. The
  identifiers `io.github.deathvenomqvp.geocollar` are placeholders to change before publishing.
- The app connects only while it is open and on the collar's screen. It does not run in the
  background or raise a notification when a collar comes into range.
- One collar at a time is connected: the one whose screen is open.
- No offline maps.
- English only.
