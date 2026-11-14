# Changelog

## 0.15.0

Needs the new collar firmware (update the collars over Wi-Fi or USB).

- **Report rate**: **Rename** now has a slider for how often the collar reports its position, from
  every 30 s to every hour (10 minutes by default). While lost, a collar still reports every 30 s.
  The collar's panel shows the rate, and a collar out of reach gets it with its next report.
- The collar takes the dog's name as its **Meshtastic name** (short name: initials, or the first
  four letters), so it shows as the dog in the Meshtastic app and on other nodes. Collars that
  already have the dog's name rename themselves after the firmware update. USB setup uses the
  same short name.

## 0.14.6

- Fixed: a collar added by **Set up only** got its first message before the base had given its
  radio the collar's key, so that message failed (PKI_FAILED). The key now goes first, the radio
  gets a moment to take it, and the base then talks to the collar straight away instead of at its
  next hello.
- PKI_FAILED (how Meshtastic 2.7 says it has no key for the collar) now makes the base give its
  radio the key again and ask the collar for it, and the collar's panel explains it.

## 0.14.5

- Fixed: a collar set up over USB could keep saying hello and never report. The base's radio can
  only send a collar encrypted messages once it has the collar's public key, and a collar set up
  on Meshtastic 2.8 has a new key and node ID that the radio hadn't heard yet. Setup now gives the
  radio the collar's key (marked verified, so a NodeInfo on the air can't replace it), and the
  base accepts reports only with that key. **Set up only** on a collar already locked to this base
  does this too. For a collar set up by hand, the base swaps NodeInfo with it when it says hello.
- A collar's panel says why the base's last message to it failed, such as no public key for it
  or no acknowledgement.

## 0.14.4

- Fixed: **Set up only** failed with "the collar restarted without these settings: the base's
  key, lock". The collar lost the lock when it came right after the Bluetooth change, in the same
  boot. Now Bluetooth and the lock each get their own restart, the lock is written once more if it
  still didn't stick, and the check after the restart covers Bluetooth too. Setup takes about a
  minute.

## 0.14.3

- Fixed: **Set up only** said it worked but changed nothing on the collar. Two things in
  Meshtastic 2.8 broke it. Setting the region on a fresh collar makes its key and changes its
  node ID to match, so every later setting went to the old ID and was dropped. And locking the
  collar (managed mode) blocked the final save. Setup now sets the region first and reconnects,
  saves each setting as it goes, locks last, then reconnects after the collar restarts and checks
  that everything stuck.
- A collar whose node ID changes during setup keeps its name, fences and history under the new ID.
- After an update (not a full install), the ID read from the chip no longer adds a collar: on
  Meshtastic 2.8 it isn't the node ID of a set-up collar. Setup finds the real one.
- Setting up from this computer's browser keeps going when the collar restarts during setup.

## 0.14.2

- **Set up a collar from this computer's browser**: under Collar firmware, **USB on this computer**
  now has **Set up only**, and **Connect and flash** sets the collar up afterwards when "Then set
  it up for this base" is ticked. The page relays the collar's USB serial port to the base app
  over a WebSocket, and the base runs the same setup as on its own USB. The connection is accepted
  only through Home Assistant's sidebar or from the page on the app's own address, because it
  carries the base's channel keys.

## 0.14.1

Needs the new collar firmware for the collar's name.

- Fixed: a collar set up over USB could stay silent. A collar reports only to a base that has
  talked to it, and the base's first message went out while the collar was restarting. Now the
  base answers a hello from a collar it already has, and the collar starts reporting.
- A freshly installed collar names itself "GeoCollar" plus the end of its node ID (instead of
  Meshtastic's "Meshtastic c3d4"), so you can tell it runs GeoCollar, until the base names it
  after the dog. Its hello is logged at info level.
- A collar that hasn't reported yet says why in its panel: not heard at all (not set up, or out of
  range), heard but not trusting this base, or heard and answered.

## 0.14.0

- **Change the base radio's settings from the page**: name, region, preset, hop limit and the
  primary channel (keep it, public default, or a new private key), plus a public LongFast channel
  for lost-dog messages. Under **Base radio**, choose **Change settings…**.
- Collars move with the base: each collar is checked first (nothing changes if one doesn't
  answer, unless you choose to leave it behind), gets the new settings over the mesh, and the base
  changes last. This works on collars locked to the base. Each moved collar shows as waiting until
  it reports on the new settings, and a collar left behind says how to set it up again.
- If the base fails to take the settings after the collars moved, **Finish the change** applies
  the same settings, including any new channel key.
- The base radio panel now says when a public channel is on a private channel's frequency, where
  strangers won't hear lost-dog messages.

## 0.13.0

Security review. Needs the new collar firmware, and the base and collars must be updated
together: older collars ignore the new command format's extra bytes, but the new firmware refuses
commands from an older base.

- Lock collars to their base: setting a collar up over USB now turns on Meshtastic's managed mode
  (by default), so no other base, phone or computer can change its settings without a full,
  erasing install. It also replaces Meshtastic's default Bluetooth PIN with a random one, shown
  in the collar's panel, and turns off the old shared-channel admin.
- No replays: commands carry a growing sequence number and the collar refuses old ones; it also
  refuses fence sets older than the ones it has. The base starts new fence versions after the
  one the collar reports, so a reinstalled base catches up.
- No spoofed reports: the base accepts status reports only from collars you added, only when
  PKI-encrypted, and only from the key it first saw from that collar. Before, any node could
  send a report and appear as a collar.
- Parcel names from imported files are shown as text in map tooltips (they could carry HTML).
- New-collar hellos are capped at 20 and written at most every 30 s per collar.
- Stricter checks on the "Set up only" port and on git refs for building from source.

## 0.12.1

- **Base radio** panel (click the radio badge): how the base radio is connected, its node,
  firmware, region, preset and channels, and warnings for anything that stops collars working
  (no region, firmware before 2.5, no public key, ham mode). Setup steps when no radio is set.
- Docs: how to set up the base radio, and USB versus network.

## 0.12.0

- Shock can now be turned on for real collars: **Turn shock on…** at the top of a collar's panel,
  with a level (1 to 30 of 99) and a confirmation tick box. The collar saves it and reports it
  back; the per-escape and per-hour limits still apply. **Turn shock off** needs no confirmation.
  Needs the new collar firmware. Replaces the simulator's own shock tick box.

## 0.11.0

- Address search on the map (top right): type an address or place and press Enter. Looked up
  with OpenStreetMap's Nominatim through the base.
- The map starts at your Home Assistant home location (Settings > System > General) when there
  are no collars to show, and a ⌂ button under ◎ goes back there. Outside Home Assistant, set
  `GEOCOLLAR_HOME` (or the **Home location** option) to "latitude, longitude".

## 0.10.0

- New collars find themselves. Flashing over USB (on the base or from this computer) reads the
  collar's Meshtastic node ID from its chip and adds it, named after the dog. On the base, it can
  then set the collar up too: trust this base (admin key), copy the base's LoRa settings and
  channels, keep Bluetooth on, and name the node. **Set up only** does that for a collar that
  already runs GeoCollar.
- Collars with the new firmware that no base has adopted say hello on the mesh; they show up under
  Collars as **New**, ready to add.
- The base's public key is shown under **Add a collar**, with a Copy button.
- Copy fences (and pulse settings) from another collar, then send them.

## 0.9.0

- Lost-dog beacon. Collars are named after the dog (Rename next to the name), and a new **If a
  dog gets lost** section holds your name and phone. 90 s after a dog goes past a line, its collar
  shows up on Bluetooth as "Rex is lost 5551234567" (90 s on, 90 s off), posts "Please help get
  Rex home. Call …" on the public Meshtastic channel every 5 minutes, and halves its GPS rate,
  until it is back inside or you press **Silence lost alert**. Needs the new collar firmware, with
  Bluetooth left enabled in Meshtastic.

## 0.8.1

- Metres / feet toggle at the top of the page, remembered per browser (feet by default in the
  US). Covers band widths, set-backs, side lengths, distances from the line, GPS notes and the
  simulator. The collar still works in whole metres, so band widths in feet round to the
  nearest metre.

## 0.8.0

- Property lines: click parcels to select one or more (neighbouring lots merge into one
  outline), then make a keep-in zone set back from the property line, either by the same
  distance all round or by a different distance for each side. The sides are numbered on the
  map and named by the way they face. A dotted line previews the perimeter as you change it.

## 0.7.1

- Fix importing a whole-county parcel file ("Maximum call stack size exceeded"). Large files now
  keep the 500 parcels nearest the selected collar, or the middle of the map.

## 0.7.0

- Property lines: import parcels from your county's GIS site as GeoJSON, KML or a shapefile
  (zipped, or the .shp, .dbf and .prj files together; reprojected from the .prj). They show as
  dashed outlines that zones snap to while drawing. Click one to make it a keep-in zone,
  simplified to the collar's 32-corner limit.

## 0.6.0

- Firmware panel: download the latest collar firmware built by CI, or build it from source on
  this machine, then flash a collar over USB (plugged into this machine or into the computer
  you are browsing from) or over Wi-Fi.
- Wi-Fi updates use Meshtastic's own OTA loader. The first install must be over USB, which puts
  the loader on the collar; after that the base can update it over the home Wi-Fi network.
- Shock label: when shock is off on the collar, the danger band row says it vibrates instead.

## 0.5.0

- Band colours: attention light blue, warning yellow, danger red.
- Repeat beep and vibrate every 2 to 10 seconds while the dog stays in a band.
- Past the line the collar stops all corrections, and stays quiet until the dog is back inside
  and clear of every band. Needs the matching collar firmware.

## 0.4.0

- Each zone now has three bands inside its line: danger (shock), warning (vibrate) and attention
  (beep), each with its own width. The danger band can be turned off per zone, so that zone never
  shocks. The map shades each band in its own colour.
- Needs collar firmware with protocol v2; older collars ignore the new messages. Fences saved
  before this version are converted (their warning distance becomes warning + attention).
- GPS accuracy note and checks for bands narrower than the GPS error or covering the whole zone.

## 0.3.0

- Simulated collars for trying fences without hardware: place one on the map, let it stay,
  wander or walk to a point at a chosen speed, with GPS error. It follows the collar firmware's
  fence rules and lists the beeps, vibrations, shocks and lost-mode changes it would make.

## 0.2.2

- Fix the street map showing "Access blocked" inside Home Assistant: send a referrer with
  map tile requests, which OpenStreetMap requires.

## 0.2.1

- Fix the Home Assistant build: always build on python:3.12-slim instead of Home Assistant's
  default Alpine base, which has no Python.

## 0.2.0

- Draw, edit and delete keep-in and keep-out zones on a satellite or street map, with checks
  against the collar's limits before sending.
- Every collar's last position and the selected collar's recent track on the map.
- Reconnects to the Meshtastic node automatically, and shows why it is not connected.
- Home Assistant: sidebar panel, option descriptions, watchdog, icon and documentation.

## 0.1.0

- First skeleton: collar list, basic fence drawing, manual commands.
