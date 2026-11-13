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
  in hardware.

## Meshtastic settings for a collar

- Private primary channel with a random 256-bit key, shared by collars and the base.
- The base node's public key in each collar's **Security > Admin keys**, so the collar accepts
  GeoCollar commands and remote admin from the base only.
- Role CLIENT (or CLIENT_MUTE) rather than TRACKER, because the collar must listen for fence
  updates and commands. Measure battery life early.
- GPS update interval: GeoCollar paces the GPS itself while fences are loaded (5 s near the line,
  up to 60 s deep inside, 15 s while lost) and puts your configured interval back when there are
  no fences. The paced value is only changed in memory, but if Meshtastic saves its config for
  another reason while pacing is active, the paced value is what gets saved.
- Turn off position broadcasts on any public channel; the collar reports its position to the base
  by encrypted direct message.
