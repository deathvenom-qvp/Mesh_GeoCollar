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

} // namespace caixianlin
} // namespace geocollar
