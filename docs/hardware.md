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

Why this and not the alternatives:

- **A 433 MHz transmitter module next to the collar** (wired to the same GPIO, same firmware) would
  leave the receiver sealed, but adds a board and an antenna, and any matching remote nearby could
  still trigger the collar. The Tracker's own SX1262 cannot help: it has no OOK mode and its
  antenna and matching are for 868/915 MHz. Keep it as the fallback if the receiver must stay
  unopened.
- **Driving the motor, buzzer and shock transistors directly** bypasses the microcontroller, which
  is what shapes the shock pulses into the transformer and times them out. Getting that wrong
  can hold the transformer on, burn the transistor or give a stronger shock than any level on the
  remote. Not supported.

With the receiver chip's output cut off, only the Tracker can command the collar: the original
remote stops working, and so does anyone else's.

### H-880B RX V1.4 receiver board

ken's collar (photos in the project thread of 22 November 2026). What is on it:

| Part | What it is |
| --- | --- |
| U4, CMT2220LS (SOP-8) with crystal Y1 | 433 MHz OOK receiver. Its data output is the line to tap. |
| U6, unmarked SOP-16 | The collar's microcontroller. |
| Transformer (back) | Shock step-up, driven by Q2/Q4 or a transistor near them. |
| M1 (back) | Vibration motor. |
| SP1 (back) | Buzzer. |
| S2 (back) | Power / pairing button. |
| B+ / B- pads, red and black leads | The collar's single-cell LiPo. |
| U1, U2, U3, R16 (0.51 Ω) | Charging and regulation, by the look of them; not identified. |
| LED strip, LED header (back) | The collar's lights. |
| P1 (back) | Two contacts by the transformer: probably the charge contacts. |

**Finding the data output.** No CMT2220LS pin table could be found. Its sibling, the CMT2210LS
(same family, SOP-8), has 1 GND, 2 RFIN, 3 VDD, 5 DOUT, 6 SDN, 8 XIN (crystal). That fits this
board: the crystal sits beside the corner pin nearest it (pin 8) and the antenna's coil and
capacitors (L1, L4, C9, C1) sit on the other row near pin 2. So DOUT is most likely **pin 5**: with
the "CMT2220LS" text upright, the top-right pin, the one nearest U6. Confirm before cutting
anything: it has continuity to a pin of U6, and it shows bursts of pulses on a scope or logic
analyzer (or in the capture below) while a remote button is held.

**Logic level.** HopeRF lists the CMT2220LS for 3.0 to 5.5 V, so on this board it may run straight
from the cell, and its output may go as high as 4.2 V. Measure, with the collar switched on:
the cell (B+ to B-), U4 pin 3, U6's supply pin (one of its corner pins, usually 1 or 16 on an
SOP-16: whichever reads the highest steady voltage) and the high level of DOUT.

- U6's supply 3.6 V or less: wire GPIO 5 through 1 kΩ straight to it.
- U6's supply higher (the cell itself, up to 4.2 V): a 3.3 V high may fall short of its input
  threshold. Put a single-gate level-shifting buffer in between, powered from U6's supply: an
  SN74LV1T34 (accepts 3.3 V in, outputs at its own supply) or a 74LVC1T45 (A side at the Tracker's
  3.3 V, B side at U6's supply).

### Wiring plan

| From (Tracker) | To (collar board) | Notes |
| --- | --- | --- |
| GPIO 5 (`GEOCOLLAR_TX_PIN`) | U6's side of DOUT: the pad U4 pin 5 sat on, once the pin is lifted | 1 kΩ in series (or the buffer above); 100 kΩ from that pad to ground so the line stays low while the Tracker boots |
| GPIO 47 (`GEOCOLLAR_SNIFF_PIN`, bench only) | The lifted U4 pin 5 itself | Direct if DOUT stays at 3.3 V or less; otherwise 10 kΩ in series and 22 kΩ to ground |
| GND | Collar B- | Common ground. Required. |
| Battery + (the cell, not 3.3 V) | Collar B+ | Only if the boards share one cell, below |
| GPIO 7 (`GEOCOLLAR_DISABLE_PIN`) | Switch to ground | Hardware off switch for every correction |

1. **Capture the remote first, before cutting anything** (next section): a wire from DOUT,
   still soldered in place, to GPIO 47 only listens, so the collar and its remote work as before.
2. Lift U4 pin 5 off its pad: heat it and lift it with a fine needle or tweezers. (Cutting the
   trace from pin 5 to U6 works too, if one is exposed.) The two outputs must never fight.
3. Wire GPIO 5 to the pad, as above. The lifted pin can stay wired to GPIO 47 for more captures.
4. Common ground, and power as below.

### Capturing the remote

Build with `GEOCOLLAR_SNIFF_PIN` set to the GPIO wired to DOUT:

```sh
PLATFORMIO_BUILD_FLAGS="-DGEOCOLLAR_SNIFF_PIN=47" firmware/build.sh
```

Then watch the serial log at 115200 baud and press each remote button in turn. For a
CaiXianLin remote the log shows each frame decoded:

```
GeoCollar: remote 0x1A2B channel 0 vibrate 35 (CaiXianLin; this collar builds as 0x4743 channel 0)
```

Anything else that looks like a remote is printed as raw high/low times, 12 pulses a line:

```
GeoCollar: unknown remote signal, 42 pulses (high/low us):
GeoCollar:   1390/770 260/740 740/260 ...
```

Send that output to the project: a remote in another protocol needs its own frame builder before
the data-line tap can work, and those times are what it is built from. The capture only listens.
The receiver chip outputs noise between frames, which keeps the CPU busy, so leave the setting off
(`-1`, the default) in the collar's real build. A logic analyzer on DOUT (any 8-channel USB one with
PulseView) shows the same thing.

### Pairing

Two ways:

- **Use the remote's ID** (simplest). Build with the transmitter ID and channel the capture logged,
  `-DGEOCOLLAR_TRANSMITTER_ID=0x1A2B -DGEOCOLLAR_CHANNEL=0`. The collar is already paired to its
  remote, so it obeys GeoCollar straight away.
- **Pair to GeoCollar's own ID** (`GEOCOLLAR_TRANSMITTER_ID`, `GEOCOLLAR_CHANNEL`). On similar
  collars you hold the power button (S2) until it beeps or the light flashes, then send any
  command: a Beep from the base app. Not confirmed for this collar.

### Bench checks

Do all of these with the shock electrodes unscrewed, or with the test lamp that came with the
collar across them, never on a person or a dog.

1. The voltages above, and which pin is DOUT.
2. A capture of every button: beep, vibrate at a low and a high level, shock at a low and a high
   level, and each channel the remote has.
3. Whether vibrate and shock last only while frames keep arriving (hold a button, then let go:
   does it stop at once?) and whether the collar stops on its own after a few seconds of a held
   button. GeoCollar relies on the first and sends a stop frame after each correction anyway.
4. After wiring: a Beep, a Vibrate and (shock turned on in the base app, test lamp fitted) a
   Shock from the base app each do what they say.
5. Current, from the shared cell: the collar board idle (its receiver chip alone is about 6 mA,
   all the time), and at a level-99 shock. Watch the Tracker's serial log for a restart during
   the shock: if it browns out, add 220 to 470 µF across the collar board's B+ and B-.

## Power and enclosure

- Both boards run from a single-cell LiPo. One larger cell with one charge port is simplest: take
  the collar's own cell off its B+ / B- pads, wire those pads to the Tracker's battery leads (the
  cell itself, not the Tracker's 3.3 V), and charge only through the Tracker's USB port, never
  through the collar's charge contacts at the same time. Keeping two cells works too, as long as
  the grounds are joined. The collar board then needs its own charging, and the line to U6 is
  driven from a board that may be powered when the collar is not (the 1 kΩ keeps that harmless).
- Run the transformer's output leads and the electrodes away from the Tracker, the GNSS antenna and
  the LoRa antenna.
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
  slowing to every 2 minutes by 100 m and every 3 minutes beyond; every 60 s while lost and in
  reach of the base or the phone app, off otherwise; a collar without that
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
  default) with Bluetooth off, or, with the Tracker or Sensor role, deep sleeps after every
  position or telemetry broadcast. The GPS is not read and the fence is not checked in that time,
  so a dog could walk out without a single correction, and a lost collar would stop its Bluetooth
  name, its help message and its checks for the base. For the same reason **Shutdown on battery
  after** is set back to off (a collar is always on battery), and the **Router** and **Router
  Late** roles, which Meshtastic treats as power saving, become **Client Base**, which still
  relays. Put right at boot (Meshtastic restarts after a change to any of them); power saving and
  the shutdown are also put right within a second of being changed. Not configurable.
- **GPS off for a lost dog out of reach.** While the dog is lost (or the lost beacon is on),
  the base has not answered the collar's reports for 2 minutes and at least 3 reports in a row,
  and no phone with the companion app has asked for anything in the last minute, the collar
  powers the GPS down (Meshtastic still looks about once an hour) and reports only every 5
  minutes, together with the help message on the public channel: each of those reports asks the
  base for an acknowledgement, so it is the check for the base. Otherwise a lost dog gets a
  fix every 60 s and reports every 30 s. The lost beacon's mesh message then carries the last position, which may be
  old. Anything from the base (the acknowledgement of a report, a message, one of its
  broadcasts), directly or relayed by the mesh, counts as an answer, and wakes the GPS for a fix
  at once, which goes to the base as soon as the collar has it; the phone app connecting wakes it
  too. While off, the collar also stops restarting a GPS
  search that finds nothing every minute. Inside the fences nothing changes: the collar enforces them without the base, so a
  base that is switched off or restarting never slows the GPS there.
- **GPS interval inside the fences** (base app, per collar: 0 to 30 s, 3 s by default), within
  35 m of the outermost band; from 35 to 100 m rising with the distance to 120 s, and 180 s
  beyond 100 m, so a big yard still saves battery. A dog that bolts from past 35 m can reach the
  line before the next fix: the owner's choice of battery over coverage there. A tick box in the
  base app keeps the owner's interval everywhere inside instead. At
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
- **When the GPS goes off.** The anchor must be heard at the threshold or stronger for 20 s, the
  dog must not be past a line or lost, and the lost beacon must be off. No GPS fix is needed
  (0.19.14 on; before, the last fix had to show the dog clear of every band, from 12 satellites,
  which near the house often never came): hearing the anchor means the dog is at the house. New
  fences end home mode, and the anchor's 20 s start again.
- **While home.** The GPS is put to sleep, with Meshtastic's GPS interval at an hour, so it still
  takes about one fix an hour. If that fix (12 satellites or more) puts the dog past a line,
  home mode ends at once; a fix in a band does not end it. The
  scan runs every 2 s. Reports say "home, GPS off" (status bit 7) and carry the last fix.
- **In Bluetooth Range.** From firmware 0.19.16 the collar also tells the base whenever it hears
  the anchor at the threshold, with the signal strength, even with the GPS on and before the 20 s
  are up (an Anchor message, protocol.md section 10). The base page and the phone app show the
  state as "In Bluetooth Range", and the collar's card shows the latest reading against the
  threshold, which helps set "How close counts as home".
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
   position on: GPS off`, and the GPS current should drop to nothing. From then on the collar
   reports only every 5 minutes, with the help message (`lost, checking whether the base can be
   reached`). Plug the radio back in: the next check (or anything the base sends) brings `base in
   reach again`, `the base answered: taking a GPS fix for it`, and once the fix is in, `fresh
   position for the base` and the dog's new position on the base map.
5. Home mode: with an anchor set, watch for `GeoCollar: home, near the anchor: GPS off` in the
   serial log, the current dropping, and `away from the anchor: GPS on` a few seconds after
   carrying the collar out of range. If the scan cannot start, the log says
   `cannot listen for the home anchor`.
