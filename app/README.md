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
   and download `geocollar-app.apk`. (Every push that changes the app, on any branch, also
   builds it: open the run of **Phone app** under the repository's Actions tab, and it is in
   `geocollar-app-android` under *Artifacts*.)
2. Open the file. Android asks whether to allow installing apps from your browser or file
   manager: allow it, then install.
3. Open GeoCollar and allow Bluetooth ("Nearby devices"), the camera, and location when it asks.

Until the repository has an Android signing key (see [Packages from GitHub](#packages-from-github)),
this is a debug build, signed with a throwaway key that changes with every build. To install a
newer one, uninstall the old one first, which also forgets the linked collars: link them again
from the base station. With the key set up, it is a signed release and each one installs over the
last.

### iPhone and iPad

Apple only lets a signed app onto a device. There are three ways:

- **Sideload the unsigned build.** Download `geocollar-app-unsigned.ipa` from the
  [app-latest release](https://github.com/deathvenom-qvp/Mesh_GeoCollar/releases/tag/app-latest)
  and install it with a sideloading tool such as AltStore or Sideloadly, which signs it with your
  own Apple ID as it installs it. With a free Apple ID it has to be refreshed every 7 days.
- **The signed build**, `geocollar-app.ipa` on the same release, once the repository has an Apple
  certificate and profile set up (see [Packages from GitHub](#packages-from-github)). With an ad
  hoc or development profile it installs on the iPhones listed in the profile; with an App Store
  profile it is for uploading to TestFlight.
- **Build it yourself** on a Mac with Xcode and an Apple ID (a free one works; the app then needs
  reinstalling every 7 days, a paid developer account lifts that):

  ```sh
  cd app
  npm ci
  npm run ios          # builds the web part, syncs it, and opens Xcode
  ```

  In Xcode, select the **App** target, pick your team under **Signing & Capabilities**, change
  the bundle identifier to one of your own, plug in the phone and press Run.

## Link your collars

1. In the base station, select any collar and press **Connect app**. Say whose phone it is.
2. In the app, tap **Add a collar** and point the camera at the QR code. Or scan it with the
   phone's own camera app, which opens GeoCollar. If no camera can be used, press **Copy the code
   as text** on the base and paste it under *Type or paste the code instead*. The app now lists
   every collar on the base.
3. Go near a collar and open it. The first time, the phone asks for that collar's Bluetooth PIN;
   the app shows it if the base knows it.

The base hands each collar its key for the phone over the mesh, with the collar's next report.
Until then the app says the collar has not been told about this phone yet, and keeps trying.

A collar added to the base later trusts the phone at once. To list it in the app, press **Add
Bella** next to the phone in the base's list and scan that code with **Add a collar**. That code
is only the list of the base's collars, with no key: it works on a phone that is linked to that
base already, and the app also drops collars the base no longer has. A code from an older base
links one collar only, as before.

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
- The phone's key is 16 random bytes, different for every phone. The base shows it once, in the
  QR code. Each collar gets its own key made from it (HMAC-SHA256 of the collar's node number),
  which the app works out too, so a key read out of one collar's memory opens no other collar.
  Removing the phone on the base removes its key from every collar.
- The app finds the collar by its Bluetooth name: the Meshtastic name (`REX_c3d4`) or, while the
  dog is lost, the beacon's name (`Rex is lost 5551234567`). A collar the phone is already
  connected to (from the phone's Bluetooth settings, the Meshtastic app, or an earlier run) stops
  advertising, so the app also looks among the phone's connected devices and uses that
  connection. After connecting it checks the collar's node number, and the collar checks the
  app's key.
- The collar takes one phone connection at a time, and the Meshtastic app and this one would
  read the same messages. If the Meshtastic app is connected to the collar, disconnect it there
  first.
- When another app or the phone itself holds the collar's Bluetooth link, the link stays up
  between this app's attempts, and the collar drops a write that repeats the one before it. So
  the app sends a heartbeat with a fresh random number before each "want config"; without it,
  every attempt after a failed one went unanswered.
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

## When it does not connect: the debug log

The app keeps a log of what it does over Bluetooth: the devices the phone is connected to and
the ones a scan sees, which one it picks, pairing, connecting, the collar's Bluetooth services,
and every Meshtastic message in and out (kind and size only), with times. It keeps the last few
thousand lines, including the previous run, and holds no keys or PINs.

Open it with **Debug log** at the bottom of the first screen or on a collar's screen. **Save or
share** hands it to the phone's share sheet as a `.txt` file (save it to Files or Drive, or send
it); **Copy** puts it on the clipboard. The log's first lines and the first screen show the build
(the GitHub run number and commit), so it is clear which build was running.

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
| `src/log.js` | The debug log. |
| `test/` | `node --test`. The link is tested against the demo collar and against the firmware's real session code. |
| `android/`, `ios/` | The native projects Capacitor wraps the web part in: permissions, icons, the `geocollar://` link. |
| `tools/make_icons.py` | Draws the icon and launch screen into both projects. |
| `tools/ios_signing.py` | Sets the iOS project up for a signed build on GitHub, from the provisioning profile. |

## Packages from GitHub

The **Phone app** workflow (`.github/workflows/app.yml`) builds the packages on every push that
changes something under `app/`, on any branch; a push that changes only the firmware or the base
does not run it. It can also be started by hand from the Actions tab (*Run workflow*). Each run
keeps its packages under *Artifacts*, and a run on `main` also replaces them on the
[app-latest release](https://github.com/deathvenom-qvp/Mesh_GeoCollar/releases/tag/app-latest).

| File | Artifact | What |
| --- | --- | --- |
| `geocollar-app.apk` | `geocollar-app-android` | Android, to install. The signed release with an Android key, else a debug build. |
| `geocollar-app.aab` | `geocollar-app-android` | Android App Bundle for Google Play. Only with an Android key. |
| `geocollar-app-debug.apk` | `geocollar-app-android` | The debug build. Only with an Android key (otherwise it is `geocollar-app.apk`). |
| `geocollar-app-unsigned.ipa` | `geocollar-app-ios` | iPhone, unsigned, for a sideloading tool. Always built. |
| `geocollar-app.ipa` | `geocollar-app-ios` | iPhone, signed. Only with an Apple certificate and profile. |
| `geocollar-app-simulator.zip` | `geocollar-app-ios` | For the iOS Simulator on a Mac: unzip, then `xcrun simctl install booted App.app`. |

The build number (Android `versionCode`, iOS build) is the workflow's run number, so each build
counts up.

### Secrets for signed builds

Without any of these the workflow still builds everything it can. Add them under the
repository's *Settings > Secrets and variables > Actions > New repository secret*.

**Android** (all four together):

| Secret | What |
| --- | --- |
| `ANDROID_KEYSTORE_BASE64` | The upload or release keystore (`.jks`), base64-encoded: `base64 -w0 release.jks` (macOS: `base64 -i release.jks`). |
| `ANDROID_KEYSTORE_PASSWORD` | The keystore's password. |
| `ANDROID_KEY_ALIAS` | The key's alias in it. |
| `ANDROID_KEY_PASSWORD` | The key's password. Leave it out if it is the same as the keystore's. |

To make a keystore: `keytool -genkeypair -v -keystore release.jks -alias geocollar -keyalg RSA
-keysize 4096 -validity 10000`. Keep a copy somewhere safe: an app signed with a lost key cannot
be updated, only uninstalled and installed again. The first signed APK cannot install over a debug
one; uninstall the debug one first.

**iPhone** (needs a paid Apple Developer account; both together):

| Secret | What |
| --- | --- |
| `IOS_CERTIFICATE_P12_BASE64` | An Apple Distribution certificate (or Apple Development, for a development profile) with its private key, exported from Keychain Access as `.p12`, base64-encoded: `base64 -i cert.p12`. |
| `IOS_CERTIFICATE_PASSWORD` | The password chosen when exporting the `.p12`. |
| `IOS_PROVISIONING_PROFILE_BASE64` | The provisioning profile (`.mobileprovision`) for the app, made with that certificate, base64-encoded: `base64 -i GeoCollar.mobileprovision`. |

The workflow takes everything else from the profile: the team, the bundle identifier (a
wildcard profile keeps `io.github.deathvenomqvp.geocollar`), and the kind of package. An *ad hoc*
profile makes an `.ipa` that installs on the iPhones registered in it (install it with Apple
Configurator, or Xcode's *Devices and Simulators* window); a *development* profile the same, for development; an
*App Store* profile one to upload to TestFlight or the App Store with Transporter. The app uses
Bluetooth, the camera and location, none of which need extra capabilities on the App ID.

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
- That the Android and iOS projects compile (on GitHub, with every push that changes the app).

Not tested, because it needs a collar and a phone:

- Everything Bluetooth: finding the collar by name or among the connected devices, pairing with its PIN on Android and on iOS,
  and Meshtastic's phone API over a real connection. `src/ble.js` has never run.
- The collar's side on hardware: answering a phone through Meshtastic's phone API, and staying
  connectable in the quiet half of the lost beacon.
- The camera, location and `geocollar://` links on a real phone.

## Not done yet

- Store listings. The GitHub workflow can make signed packages (see
  [Packages from GitHub](#packages-from-github)) but no signing keys are set up yet, and nothing
  uploads to Google Play or TestFlight. The identifiers `io.github.deathvenomqvp.geocollar` are
  placeholders to change before publishing.
- The app connects only while it is open and on the collar's screen. It does not run in the
  background or raise a notification when a collar comes into range.
- One collar at a time is connected: the one whose screen is open.
- No offline maps.
- English only.
