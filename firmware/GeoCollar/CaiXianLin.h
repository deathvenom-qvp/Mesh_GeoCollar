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

/// One frame read back off the receiver chip's output (see Decoder).
struct DecodedFrame {
    uint16_t transmitterId;
    uint8_t channel;
    CollarAction action;
    uint8_t intensity;
};

/// Reads CaiXianLin frames out of the pulses on the collar receiver chip's data output, so the
/// collar can learn the original remote's transmitter ID and confirm the timing on the bench
/// (GEOCOLLAR_SNIFF_PIN). Feed it every pulse as it ends: how long the line was high, then how
/// long it was low after. Timing is accepted within about a third of the nominal values, since
/// the receiver chip stretches and shortens pulses. Noise between frames just resets it.
class Decoder
{
  public:
    /// Returns true, and fills out, when this pulse completes a frame whose checksum and
    /// postamble are right. The last bit's low time runs into the gap before the next frame,
    /// so any low time over 400 us ends a frame.
    bool feed(uint32_t highUs, uint32_t lowUs, DecodedFrame &out);
    void reset() { count_ = 0; }

  private:
    uint8_t count_ = 0; // 0: waiting for a sync pulse; n > 0: sync and n - 1 bits so far
    uint64_t bits_ = 0;
};

/// Short name for an action, for logs.
const char *actionName(CollarAction action);

} // namespace caixianlin

/// Collects a run of pulses that all look like remote-control signalling (each high and low time
/// between 100 us and 5 ms), so that a remote whose protocol the decoders do not know can still
/// be captured and its timing printed. The receiver chip's noise rarely makes a long run.
class BurstRecorder
{
  public:
    static constexpr size_t kMaxPulses = 96;
    static constexpr size_t kMinPulses = 24; // a shorter run is taken for noise

    /// Add one pulse. Returns true when it ended a run worth printing (then see pulses(),
    /// count()); call clear() after printing.
    bool feed(uint32_t highUs, uint32_t lowUs);
    const Pulse *pulses() const { return pulses_; }
    size_t count() const { return count_; }
    void clear() { count_ = 0; }

  private:
    Pulse pulses_[kMaxPulses];
    size_t count_ = 0;
};
} // namespace geocollar
