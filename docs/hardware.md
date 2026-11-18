# Hardware and wiring

One collar = a **Heltec Wireless Tracker V1.1** (ESP32-S3, SX1262 LoRa, UC6580 dual-band GNSS)
running Meshtastic with the GeoCollar module, wired into the receiver board of a
**CaiXianLin-type training collar**.

## Pin plan (Wireless Tracker V1.1)

| GPIO | Use | Wiring |
| --- | --- | --- |
| 5 | Data to the collar's microcontroller (`GEOCOLLAR_TX_PIN`) | 1 kΩ in series, 100 kΩ to ground at the collar end so the line stays low while the Tracker boots |
| 7 | Hardware disable switch (`GEOCOLLAR_DISABLE_PIN`) | Switch to ground; internal pull-up, so no switch means corrections stay enabled |
| 6 | Reserved: collar power on/off | Gate of a high-side load switch or P-MOSFET feeding the collar board (not in firmware yet) |
| 4 | Reserved: collar battery sense | Divider to ADC1, only if the collar keeps its own battery (not in firmware yet) |
| GND | Common ground | Tracker ground to collar ground |

Why these: GPIO 4 to 7 are not used by Meshtastic on this board, are not ESP32-S3 boot strapping
pins, and are on ADC1, which still works while Wi-Fi is on. Pins to avoid:

- 0, 3, 45, 46: boot strapping pins (0 is also the user button, 3 powers the GNSS).
- 1, 2: battery voltage sense. 8 to 14: LoRa radio. 18: LED. 19, 20: USB.
- 21, 38 to 42: screen. 33 to 36: GNSS. 43, 44: serial console. 26 to 32: internal flash.
- 47 and 48 are also free and can be spares, but are not on ADC.

The GPIO numbers come from Meshtastic's board definition for this Tracker
(`variants/esp32s3/heltec_wireless_tracker/variant.h`). Heltec's pin map image could not be
checked here, so confirm 4 to 7 are on your board's headers (the silkscreen shows the numbers)
before soldering.

## The data-line tap

The training collar's receiver has a 433 MHz receiver chip whose demodulated output goes to the
collar's microcontroller. GeoCollar drives that line directly from `GEOCOLLAR_TX_PIN` (GPIO 5 by
default), so the collar's own microcontroller decodes the commands and its built-in limits and
timeouts still apply. Nothing is transmitted on 433 MHz.

1. Open one receiver and find the receiver chip's data output pin. Confirm it with a logic analyzer
   while pressing the remote's buttons: you should see a 1400 µs high / 750 µs low sync pulse, then
   43 bits of 1000 µs each.
2. Note the logic voltage on that line. The Tracker's GPIOs are 3.3 V; if the collar runs lower,
   add a level shifter or a resistor divider.
3. Isolate the receiver chip's output from the line (lift the pin or cut the trace) so the two
   outputs never fight, then wire the Tracker's GPIO and a common ground to the microcontroller side.
4. Pair the collar to GeoCollar's transmitter ID (`GEOCOLLAR_TRANSMITTER_ID`, channel
   `GEOCOLLAR_CHANNEL`). The pairing steps are not documented for this collar; on similar collars
   you hold the power button until it beeps, then send any command.

Still to confirm on the bench: the pairing steps, whether shock and vibrate last only while frames
keep arriving, and the collar's battery voltage.

## Power and enclosure

- Both boards run from a single-cell LiPo. One larger cell with one charge port is simplest; keep
  a common ground.
- Put the GNSS antenna on top of the neck with a view of the sky; keep the LoRa antenna away from
  the shock electrodes.
- Plan a new sealed enclosure: the Tracker will not fit the original receiver housing.
- Optional: a switch from GPIO 7 (`GEOCOLLAR_DISABLE_PIN`) to ground turns off every correction
  in hardware: the fence's own, and beeps, vibrations and shocks sent from the base.

## Meshtastic settings for a collar

- Private primary channel with a random 256-bit key, shared by collars and the base.
- The base node's public key in each collar's **Security > Admin keys**, so the collar accepts
  GeoCollar commands and remote admin from the base only.
- Role: leave it at Meshtastic's default, Client. GeoCollar runs it as **Client Mute**, which
  sends and receives as usual but does not relay other nodes' packets (see Battery life). Do not
  pick Tracker or Sensor: they are meant to sleep between reports, and the collar must listen for fence updates and commands.
- GPS update interval: GeoCollar paces the GPS itself while fences are loaded (inside the fences
  the interval set per collar in the base app, 3 s unless changed, within 35 m of the bands,
  slowing to every 2 minutes by 100 m and every 3 minutes beyond; every 15 s while lost; a collar without that
  setting fixes every 5 s near the line) and puts your configured
  interval back when there are no fences. Meshtastic saves the paced value with the rest of its
  config when it shuts down or a setting changes, so the collar keeps your own interval in its
  state file and puts that back after a restart. Change the interval in the Meshtastic app while
  the collar is running and it becomes the new value to put back.
- GPS power: on the Wireless Tracker one switch (Vext, GPIO 3) powers the GPS, its antenna
  amplifier and the screen. Meshtastic 2.8 switches it off whenever the screen is dark and the
  GPS is asleep, which it is between fixes more than 10 s apart (deep inside the fences). Leave
  **GPS EN GPIO** at 0: the board support already drives Vext, and a second driver on GPIO 3 would
  fight it.
- Screen: GeoCollar shortens Meshtastic's default screen timeout (10 minutes) to 1 second. A
  timeout you set yourself is kept, except 30 s, which earlier GeoCollar versions set.
- Leave **Power saving** off: GeoCollar turns it off anyway (see Battery life).
- Turn off position broadcasts on any public channel; the collar reports its position to the base
  by encrypted direct message.

## Battery life

Nothing below has been measured on a collar yet. The currents are typical figures from the
chips' datasheets and from reading Meshtastic 2.8's code, to show where the battery goes.

| What | Typical draw | What GeoCollar does |
| --- | --- | --- |
| GPS (UC6580, dual band) and its antenna amplifier | Tens of mA while on: the largest single draw | Inside the fences, a fix as often as set in the base app (always on at 10 s or less, asleep between fixes above that); off near the home anchor; slowed while lost and out of reach. |
| ESP32-S3 | About 20 to 30 mA at 80 MHz | Meshtastic already runs it at 80 MHz. Keeping Wi-Fi off keeps it there. |
| Wi-Fi | Around 100 mA, and the CPU at 240 MHz | Kept off (below). |
| Screen backlight | Tens of mA while lit | Lit for 1 s instead of 30 s after a boot, button press or text message. |
| Bluetooth advertising | A few mA at Meshtastic's 30 to 60 ms interval | Every second instead, while no phone is connected. |
| LoRa receive (SX1262) | About 5 mA, all the time | Unchanged: the collar has to hear the base. |
| LoRa transmit | About 120 mA for each packet's airtime (around a second at the default preset) | No relaying of other nodes' packets. |

What GeoCollar changes on the collar, and why (each can be turned off with a build flag in
`firmware/GeoCollar/GeoCollarConfig.h`):

- **Wi-Fi off** (`GEOCOLLAR_WIFI_OFF`). Nothing on a collar has used Wi-Fi since 0.17.0, and the
  base removes the network it saved for Wi-Fi updates. With Wi-Fi on, Meshtastic runs the CPU at
  240 MHz, keeps the Wi-Fi radio from sleeping, and cannot start Bluetooth, so the lost beacon
  would have no Bluetooth name. A collar that still has Wi-Fi on has it turned off and saved at
  boot, and restarts once so Bluetooth can start.
- **No relaying** (`GEOCOLLAR_NO_RELAY`). With the default Client role a node repeats every packet
  it hears, at full power. On a busy public mesh that can be many transmissions an hour; on a
  quiet one it costs nothing. As Client Mute the collar still sends its reports, the lost-dog
  message and its replies, and other nodes still relay for it. Only the default role is changed.
  If you have two collars and one reaches the base only through the other, set that one to a
  role other than Client in the Meshtastic app, or build with `GEOCOLLAR_NO_RELAY=0`.
- **Screen** (`GEOCOLLAR_SCREEN_ON_SECS`). The screen is dark except for a second after a boot, a
  button press or a text message on any of the collar's channels. Plugged into USB with the base
  app connected, Meshtastic keeps it lit as before.
- **Slower Bluetooth advertising** (`GEOCOLLAR_BLE_ADV_MS`). The Meshtastic app and the GeoCollar
  app still find the collar within a second or two. While a phone is connected, and while the
  lost beacon runs, Bluetooth is left as it was.
- **Meshtastic power saving forced off.** On an ESP32 it sleeps for minutes at a time (5 by
  default) with Bluetooth off. The GPS is not read and the fence is not checked in that time, so
  a dog could walk out without a single correction. Not configurable.
- **GPS slowed for a lost dog out of reach.** While the dog is lost (or the lost beacon is on),
  the base has not answered the collar's reports for 2 minutes and at least 3 reports in a row,
  and no phone with the companion app has asked for anything in the last minute, the collar
  takes a fix every 5 minutes instead of every 15 or 30 s. Nothing could pass a fresher position
  on, except the lost beacon's mesh message, which goes every 5 minutes anyway. Anything from the
  base (the acknowledgement of a report, a message, one of its broadcasts), directly or relayed
  by the mesh, counts as an answer, and wakes the GPS for a fix at once. While slowed, the collar
  also stops restarting a GPS search that finds nothing every minute, and leaves the timing to
  Meshtastic. Inside the fences nothing changes: the collar enforces them without the base, so a
  base that is switched off or restarting never slows the GPS there.
- **GPS interval inside the fences** (base app, per collar: 0 to 30 s, 3 s by default), within
  35 m of the outermost band; from 35 to 100 m rising with the distance to 120 s, and 180 s
  beyond 100 m, so a big yard still saves battery. A dog that bolts from past 35 m can reach the
  line before the next fix: the owner's choice of battery over coverage there. At
  10 s or less Meshtastic keeps the GPS on all the time. Above that it powers the GPS down
  between fixes, but each fix costs the restart plus a hold after the lock (half of what is
  above 10 s, at most 20 s; GeoCollar shortens the interval it gives Meshtastic by that much), and
  Meshtastic does not power it down at all if its recent lock times are as long as the gap. So
  at 30 s the GPS is on for about 10 s plus the restart, at 20 s about 5 s plus the restart. If
  the restart is cold (30 s or more, see the bench check below), anything up to 30 s is in effect
  always on. Home mode is the larger saving for a dog that spends time indoors.
- **GPS fixes on time.** For an interval over 10 s Meshtastic holds the GPS on for up to 20 s
  after each lock before it reports the fix, and counts the interval from then. GeoCollar now
  asks for an interval that much shorter, so fixes really come as often as the pacing promises
  (every 60 s deep inside, not every 80 s). This keeps the GPS on a little more than before: it
  is a fix for correction timing, not a saving.

### Home mode

Off unless you turn it on (Settings > Home mode in the base app). The idea: Bluetooth only
reaches a short way, so a collar that hears an anchor in the house strongly is well inside its
fences, and needs no GPS. Listening costs a few mA at most (a 100 ms scan window every 2 to 5 s),
against tens of mA for the GPS, so a dog that spends most of the day indoors should run much
longer.

- **What it listens for.** Either the base station's radio (its Meshtastic Bluetooth name, so
  nothing to set up, but the radio must have Bluetooth on, be connected to the base by USB rather
  than Wi-Fi, which turns Meshtastic's Bluetooth off, and have no phone connected to it over
  Bluetooth, which stops it advertising), or a Bluetooth beacon by its address: any iBeacon or
  Eddystone tag, or a spare Meshtastic radio, put where the dog spends its time. Nothing else is
  needed on the beacon.
- **When the GPS goes off.** The anchor must be heard at the threshold or stronger for 20 s,
  the collar's last fix must have shown the dog inside and clear of every band (and be at most
  10 minutes old, while the anchor has been heard), and the lost beacon must be off. New fences
  also need a fresh fix first.
- **While home.** The GPS is put to sleep, with Meshtastic's GPS interval at an hour, so it still
  takes about one fix an hour. If that fix is not inside and clear, home mode ends at once. The
  scan runs every 2 s. Reports say "home, GPS off" (status bit 7) and carry the last fix.
- **When it ends.** Once the anchor has not been heard within 6 dB of the threshold for 7 s, the
  GPS is woken and fixes as often as it does near the line, until a fix says where the dog is.
  Turning Bluetooth off on the collar, a change of anchor, or the fences going away end it too.
- **The catch: the restart.** With the screen dark Vext is off while the GPS sleeps, so the GPS
  loses power and may need a cold start. A dog leaving at a steady run (4 m/s) covers about
  4 × (7 + restart time) metres before its first fix, so the anchor should be at least that far,
  plus its range, inside the bands. The base app checks this for each collar with the restart
  time you choose (30 s unless you have timed it, about 150 m). Time it on the bench (below), and
  pick "Very close" for a small yard.
- **Spoofing.** Bluetooth names and addresses are easy to copy. Someone who copies the anchor
  near a collar keeps its GPS off while it is close by, for up to an hour per fix. That is no
  worse than walking off with the dog, but it is why home mode is off by default.

Signal strength is rough: walls, the dog's body and the collar's angle move readings by 10 dB or
more. The three choices in the app are -65 dBm (the same room), -75 (in or right by the house)
and -85 (the yard around the anchor); the circle on the map is a guess from typical figures.

Left as they are, and why:

- **The GPS rate near the line.** It is what makes corrections arrive in time (3 s within 35 m
  of the bands, or as set in the base app).
- **CPU below 80 MHz, or automatic light sleep.** Bluetooth, the GPS serial port and the
  correction transmitter's timing rely on it. Untestable without hardware.
- **Duty-cycled LoRa receive.** It would miss fence updates and commands from the base.
- **Bluetooth off.** The lost beacon, the phone app and home mode need it.
- **Fewer GPS bands or satellite systems.** It would save some GPS current but cost accuracy near
  trees and buildings, which is where false corrections come from. Worth trying on the bench.
- **Meshtastic's own position and telemetry broadcasts.** A few short transmissions an hour.

### Measuring on the bench

1. Power the collar through a USB power meter or a bench supply with a current readout, with no
   USB data connection (a data connection keeps Meshtastic awake and the screen lit).
2. Note the current deep inside the fences (over 100 m from the bands: fixes every 3 minutes)
   and near the line (GPS always on).
3. In a serial log, `Took Ns to get lock` after each GPS wake-up says whether the UC6580 restarts
   warm (a few seconds) or cold (30 s or more) once Vext has been off. If it is cold, deep-inside
   pacing saves little, and the GPS could as well stay on. It is also the restart time to pick
   for home mode.
4. Out of reach: force a collar lost from the base, then unplug the base radio. After about 2
   minutes the serial log says `GeoCollar: base out of reach` and `lost and nothing can pass the
   position on`, and the GPS current should drop to a short burst every 5 minutes. Plug the radio
   back in: the next acknowledged report brings `base in reach again` and a fix straight away.
5. Home mode: with an anchor set, watch for `GeoCollar: home, near the anchor: GPS off` in the
   serial log, the current dropping, and `away from the anchor: GPS on` a few seconds after
   carrying the collar out of range. If the scan cannot start, the log says
   `cannot listen for the home anchor`.
