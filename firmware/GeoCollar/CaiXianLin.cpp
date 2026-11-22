// SPDX-License-Identifier: GPL-3.0-or-later
// See CaiXianLin.h for protocol credit (OpenShock project).

#include "CaiXianLin.h"

namespace geocollar
{
namespace caixianlin
{

uint8_t checksum(uint32_t payload)
{
    return static_cast<uint8_t>(((payload >> 24) & 0xFF) + ((payload >> 16) & 0xFF) + ((payload >> 8) & 0xFF) + (payload & 0xFF));
}

uint32_t payload(uint16_t transmitterId, uint8_t channel, CollarAction action, uint8_t intensity)
{
    if (intensity > kMaxIntensity)
        intensity = kMaxIntensity;
    if (action == CollarAction::Beep)
        intensity = 0;
    return (static_cast<uint32_t>(transmitterId) << 16) | (static_cast<uint32_t>(channel & 0x0F) << 12) |
           (static_cast<uint32_t>(static_cast<uint8_t>(action) & 0x0F) << 8) | intensity;
}

bool buildFrame(Pulse *out, uint16_t transmitterId, uint8_t channel, CollarAction action, uint8_t intensity)
{
    if (channel > kMaxChannel)
        return false;

    const uint32_t p = payload(transmitterId, channel, action, intensity);
    // 40 data bits (payload then checksum), shifted left by 3 to append the zero postamble.
    const uint64_t bits = ((static_cast<uint64_t>(p) << 8) | checksum(p)) << 3;

    out[0] = kSync;
    for (size_t i = 0; i < 43; i++) {
        const bool one = (bits >> (42 - i)) & 1u;
        out[1 + i] = one ? kOne : kZero;
    }
    return true;
}

namespace
{
bool within(uint32_t us, uint32_t lo, uint32_t hi)
{
    return us >= lo && us <= hi;
}
} // namespace

bool Decoder::feed(uint32_t highUs, uint32_t lowUs, DecodedFrame &out)
{
    // Sync: nominally 1400 us high, 750 low.
    const bool sync = within(highUs, 1000, 1900) && within(lowUs, 450, 1100);
    if (count_ == 0) {
        if (sync) {
            count_ = 1;
            bits_ = 0;
        }
        return false;
    }

    const uint8_t bit = count_ - 1; // index of this bit, 0..42
    const bool last = bit == 42;
    // Bits: 750/250 is a one, 250/750 a zero; each about 1000 us in all.
    const bool shapeOk = within(highUs, 120, 1100) && (last ? lowUs >= 120 : within(lowUs, 120, 1100)) &&
                         (last || within(highUs + lowUs, 700, 1350));
    if (!shapeOk) {
        count_ = 0;
        if (sync) { // a new frame may start right here
            count_ = 1;
            bits_ = 0;
        }
        return false;
    }
    // The last bit's low time runs into the gap, so judge it by its high time alone.
    const bool one = last ? highUs >= 500 : highUs > lowUs;
    bits_ = (bits_ << 1) | (one ? 1u : 0u);
    count_++;
    if (!last)
        return false;

    count_ = 0;
    if (lowUs < 400 || (bits_ & 0x7) != 0)
        return false; // too short a gap, or the three-bit zero postamble is wrong
    const uint32_t p = static_cast<uint32_t>(bits_ >> 11);
    if (static_cast<uint8_t>(bits_ >> 3) != checksum(p))
        return false;
    const uint8_t action = (p >> 8) & 0x0F;
    if (action < 1 || action > 3)
        return false;
    out.transmitterId = static_cast<uint16_t>(p >> 16);
    out.channel = (p >> 12) & 0x0F;
    out.action = static_cast<CollarAction>(action);
    out.intensity = p & 0xFF;
    return true;
}

const char *actionName(CollarAction action)
{
    switch (action) {
    case CollarAction::Shock:
        return "shock";
    case CollarAction::Vibrate:
        return "vibrate";
    case CollarAction::Beep:
        return "beep";
    }
    return "?";
}

} // namespace caixianlin

bool BurstRecorder::feed(uint32_t highUs, uint32_t lowUs)
{
    const bool highOk = highUs >= 100 && highUs <= 5000;
    const bool lowOk = lowUs >= 100 && lowUs <= 5000;
    if (highOk && count_ < kMaxPulses)
        pulses_[count_++] = {static_cast<uint16_t>(highUs), static_cast<uint16_t>(lowUs > 65535 ? 65535 : lowUs)};
    // A run ends with a pulse out of range (most often the long low after the remote lets go),
    // or when the buffer is full.
    if (highOk && lowOk && count_ < kMaxPulses)
        return false;
    if (count_ >= kMinPulses)
        return true;
    count_ = 0;
    return false;
}

} // namespace geocollar
