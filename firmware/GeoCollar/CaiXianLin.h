// SPDX-License-Identifier: GPL-3.0-or-later
//
// Frame builder for CaiXianLin-type 433.95 MHz training collars.
//
// Credit: the protocol was reverse engineered and documented by the OpenShock project
// (https://wiki.openshock.org/hardware/shockers/caixianlin). The frame layout here, including the
// 3-bit zero postamble and the Sum8 checksum, matches OpenShock's CaiXianlinEncoder
// (https://github.com/OpenShock/Firmware, AGPL-3.0). This file is an independent implementation
// written from that documentation; no OpenShock source is copied.
//
// This file is pure C++ with no Arduino dependencies so it can be unit tested on a PC.

#pragma once

#include <cstddef>
#include <cstdint>

namespace geocollar
{

enum class CollarAction : uint8_t {
    Shock = 1,
    Vibrate = 2,
    Beep = 3,
};

/// One on/off pulse pair: the line is held high for highUs, then low for lowUs.
struct Pulse {
    uint16_t highUs;
    uint16_t lowUs;
};

namespace caixianlin
{

constexpr Pulse kSync = {1400, 750};
constexpr Pulse kOne = {750, 250};
constexpr Pulse kZero = {250, 750};

constexpr uint8_t kMaxIntensity = 99;
constexpr uint8_t kMaxChannel = 2;

/// Sync pulse + 32 payload bits + 8 checksum bits + 3 postamble zeros.
constexpr size_t kFrameLength = 44;

/// Total airtime of one frame in microseconds (about 45 ms).
constexpr uint32_t kFrameUs = (kSync.highUs + kSync.lowUs) + 43u * 1000u;

/// Sum of the four payload bytes, modulo 256.
uint8_t checksum(uint32_t payload);

/// Packs [transmitterId:16][channel:4][action:4][intensity:8].
/// Intensity is clamped to 0..99, and forced to 0 for Beep (some collars lock up otherwise).
uint32_t payload(uint16_t transmitterId, uint8_t channel, CollarAction action, uint8_t intensity);

/// Fills out[kFrameLength] with the pulses for one frame. Returns false for an invalid channel.
bool buildFrame(Pulse *out, uint16_t transmitterId, uint8_t channel, CollarAction action, uint8_t intensity);

} // namespace caixianlin
} // namespace geocollar
