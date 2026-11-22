// SPDX-License-Identifier: GPL-3.0-or-later
//
// Build-time settings. Override any of these with -D flags in the build (see firmware/build.sh).

#pragma once

// Heltec Wireless Tracker V1.1 pin plan (see docs/hardware.md). Meshtastic already uses
// 0 (button), 1-2 (battery sense), 3 (Vext), 8-14 (LoRa), 18 (LED), 19-20 (USB), 21 and 38-42
// (screen), 33-36 (GNSS), 43-44 (serial console) and 45-46 (I2C, also boot straps). GPIO 4-7
// are free, not boot straps, and on ADC1, so GeoCollar uses those.

// GPIO wired into the training collar receiver's data line (the pin between its 433 MHz
// receiver chip and its microcontroller).
#ifndef GEOCOLLAR_TX_PIN
#define GEOCOLLAR_TX_PIN 5
#endif

// Bench tool: a spare GPIO wired to the collar receiver chip's data output (through a divider if
// that line goes above 3.3 V), to log what the original remote sends and learn its transmitter ID.
// Listening only; off (-1) unless set, because the chip's noise keeps the CPU busy. See
// docs/hardware.md, "Capturing the remote".
#ifndef GEOCOLLAR_SNIFF_PIN
#define GEOCOLLAR_SNIFF_PIN -1
#endif

// Optional switch to ground that disables all corrections in hardware (internal pull-up, so
// leaving it unconnected means "enabled"). -1 = do not read any pin.
#ifndef GEOCOLLAR_DISABLE_PIN
#define GEOCOLLAR_DISABLE_PIN 7
#endif

// Reserved, not used by the firmware yet: GPIO 6 to switch the collar board's power through a
// load switch, GPIO 4 to read the collar's own battery through a divider if it keeps its cell.

// The transmitter ID and channel this collar's receiver is paired to. Pick your own ID.
#ifndef GEOCOLLAR_TRANSMITTER_ID
#define GEOCOLLAR_TRANSMITTER_ID 0x4743 // "GC"
#endif
#ifndef GEOCOLLAR_CHANNEL
#define GEOCOLLAR_CHANNEL 0
#endif

// How often to send a status report to the base, by state.
#ifndef GEOCOLLAR_STATUS_INTERVAL_MS
#define GEOCOLLAR_STATUS_INTERVAL_MS (10 * 60 * 1000)
#endif
#ifndef GEOCOLLAR_LOST_STATUS_INTERVAL_MS
#define GEOCOLLAR_LOST_STATUS_INTERVAL_MS (30 * 1000)
#endif

// Battery life (see docs/hardware.md, Battery life). Set any of these to 0 to keep Meshtastic's own
// behaviour.
//
// Keep Wi-Fi off. Nothing on a collar uses it since 0.17.0, and with it on Meshtastic runs the CPU
// at 240 MHz instead of 80 and cannot start Bluetooth, which the lost beacon needs.
#ifndef GEOCOLLAR_WIFI_OFF
#define GEOCOLLAR_WIFI_OFF 1
#endif
// Do not relay other nodes' packets: with Meshtastic's default role (Client) a collar repeats
// every packet it hears, at full transmit power. Only the default role is changed: any other
// role chosen in the Meshtastic app is kept.
#ifndef GEOCOLLAR_NO_RELAY
#define GEOCOLLAR_NO_RELAY 1
#endif
// Seconds the screen stays lit after a button press, boot or text message, while Meshtastic's
// timeout is its default (or the 30 s that GeoCollar 0.16 to 0.19 set).
#ifndef GEOCOLLAR_SCREEN_ON_SECS
#define GEOCOLLAR_SCREEN_ON_SECS 1
#endif
// Bluetooth advertising interval while no phone is connected and the lost beacon is off, ms.
// Meshtastic's own is 30 to 60 ms.
#ifndef GEOCOLLAR_BLE_ADV_MS
#define GEOCOLLAR_BLE_ADV_MS 1000
#endif

// After a correction, keep sending the stop frame this long so the collar really stops.
#define GEOCOLLAR_STOP_MS 300
// Longest single correction the firmware will ever send, whatever the base asks for.
#define GEOCOLLAR_MAX_CORRECTION_MS 3000
