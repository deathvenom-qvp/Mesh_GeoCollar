// SPDX-License-Identifier: GPL-3.0-or-later
//
// Build-time settings. Override any of these with -D flags in the build (see firmware/build.sh).

#pragma once

// GPIO wired into the training collar receiver's data line (the pin between its 433 MHz
// receiver chip and its microcontroller). GPIO 5 is free on the Heltec Wireless Tracker V1.1
// header; confirm against your board before wiring.
#ifndef GEOCOLLAR_TX_PIN
#define GEOCOLLAR_TX_PIN 5
#endif

// Optional switch to ground that disables all corrections in hardware. -1 = not fitted.
#ifndef GEOCOLLAR_DISABLE_PIN
#define GEOCOLLAR_DISABLE_PIN -1
#endif

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

// After a correction, keep sending the stop frame this long so the collar really stops.
#define GEOCOLLAR_STOP_MS 300
// Longest single correction the firmware will ever send, whatever the base asks for.
#define GEOCOLLAR_MAX_CORRECTION_MS 3000
