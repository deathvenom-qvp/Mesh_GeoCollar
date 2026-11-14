// SPDX-License-Identifier: GPL-3.0-or-later
//
// Wire format for GeoCollar messages between collars and the base station.
// Little-endian, versioned, small enough for one LoRa packet. See docs/protocol.md.
// The base station's Python codec (base/geocollar_base/protocol.py) must match this file.

#pragma once

#include "Fence.h"

#include <cstddef>
#include <cstdint>

namespace geocollar
{
namespace proto
{

/// Meshtastic portnum for GeoCollar traffic. Private apps use 256 and up.
constexpr uint16_t kPortNum = 300;
constexpr uint8_t kVersion = 2; // 2: zones carry danger / warning / attention band widths

/// Largest chunk of fence data per packet, leaving room for headers inside Meshtastic's payload.
constexpr size_t kMaxChunkData = 180;
/// Largest encoded fence set, to bound reassembly memory.
constexpr size_t kMaxFenceBlob = 1100;
/// Longest correction pulse interval, seconds.
constexpr uint8_t kMaxPulseS = 10;

enum class MsgType : uint8_t {
    FenceChunk = 1, // base -> collar
    Status = 2,     // collar -> base
    Breach = 3,     // collar -> base
    Command = 4,    // base -> collar
    Owner = 5,      // base -> collar: the dog's name and the owner's contact, for the lost beacon
    Hello = 6,      // collar -> broadcast: "I run GeoCollar and no base has adopted me yet"
};

enum class CommandAction : uint8_t {
    Shock = 1,
    Vibrate = 2,
    Beep = 3,
    CorrectionsOff = 10,
    CorrectionsOn = 11,
    ForceLost = 12,
    ClearLost = 13,
    RequestStatus = 14,
    SilenceLost = 15, // stop the lost beacon until the dog is next back inside
    ShockOn = 16,     // allow shock, at level `intensity` (1..99, capped by the collar's maximum)
    ShockOff = 17,
};

struct Command {
    CommandAction action;
    uint8_t intensity;
    uint16_t durationMs;
};

struct FenceChunk {
    uint16_t fenceVersion;
    uint8_t index;
    uint8_t count;
    const uint8_t *data;
    size_t length;
};

struct StatusReport {
    uint16_t fenceVersion;
    uint8_t state;      // CollarState
    uint8_t batteryPct; // 0..100, 255 = unknown
    int32_t latI;
    int32_t lonI;
    uint16_t pdopX100;
    uint8_t sats;
    int16_t clearanceM; // positive = allowed side
    uint8_t flags;      // bit0 corrections enabled, bit1 shock enabled, bit2 lost beacon on, bit3 beacon silenced
};

constexpr size_t kMaxDogName = 24;
constexpr size_t kMaxOwnerName = 24;
constexpr size_t kMaxPhone = 20;

/// Who the dog is and whom to call, for the lost beacon. NUL-terminated UTF-8.
struct OwnerInfo {
    char dog[kMaxDogName + 1] = {};
    char owner[kMaxOwnerName + 1] = {};
    char phone[kMaxPhone + 1] = {};
};

constexpr size_t kStatusLength = 2 + 2 + 1 + 1 + 4 + 4 + 2 + 1 + 2 + 1;
constexpr size_t kCommandLength = 2 + 1 + 1 + 2;

/// Peek at a message's type. Returns false if the buffer is not a GeoCollar v1 message.
bool messageType(const uint8_t *buf, size_t len, MsgType &type);

size_t encodeStatus(uint8_t *out, size_t cap, MsgType type, const StatusReport &s);
bool decodeStatus(const uint8_t *buf, size_t len, StatusReport &s);

/// Hello: [version][6][flags: bit0 has an admin key, bit1 has fences][battery %, 255 unknown]
constexpr size_t kHelloSize = 4;
size_t encodeHello(uint8_t *out, size_t cap, bool hasAdminKey, bool hasFences, uint8_t batteryPct);
size_t encodeOwner(uint8_t *out, size_t cap, const OwnerInfo &o);
bool decodeOwner(const uint8_t *buf, size_t len, OwnerInfo &o);

size_t encodeCommand(uint8_t *out, size_t cap, const Command &c);
bool decodeCommand(const uint8_t *buf, size_t len, Command &c);

size_t encodeFenceChunk(uint8_t *out, size_t cap, const FenceChunk &c);
bool decodeFenceChunk(const uint8_t *buf, size_t len, FenceChunk &c);

/// Fence set blob (what the chunks reassemble into).
size_t encodeFenceSet(uint8_t *out, size_t cap, const FenceSet &f);
bool decodeFenceSet(const uint8_t *buf, size_t len, uint16_t version, FenceSet &f);

/// Reassembles chunks of one fence version. Chunks may arrive in any order; a chunk with a new
/// version discards a partial older one.
class FenceAssembler
{
  public:
    /// Returns true when the set for c.fenceVersion is complete; then read data() and length().
    bool add(const FenceChunk &c);
    const uint8_t *data() const { return buf_; }
    size_t length() const { return length_; }
    uint16_t version() const { return version_; }

  private:
    void reset(uint16_t version, uint8_t count);
    uint8_t buf_[kMaxFenceBlob] = {};
    size_t chunkLen_[8] = {};
    uint8_t count_ = 0;
    uint8_t have_ = 0; // bitmask of received chunks
    uint16_t version_ = 0;
    size_t length_ = 0;
    bool active_ = false;
};

} // namespace proto
} // namespace geocollar
