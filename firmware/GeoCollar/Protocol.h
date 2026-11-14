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
    Config = 7,     // base -> collar: how often to report
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
    // Increases with every command from the base (it is the base's clock in seconds, or one more
    // than the last). The collar refuses a number it has already passed, so a recorded command
    // cannot be played back to it later.
    uint32_t seq;
};

/// True when fence version `incoming` is the same as or newer than `current` (16-bit, wrapping),
/// so an old fence set recorded off the air cannot replace newer fences.
inline bool fenceVersionAcceptable(uint16_t incoming, uint16_t current)
{
    return current == 0 || static_cast<int16_t>(incoming - current) >= 0;
}

/// What to do with a complete fence set from the base.
enum class FenceUpdate : uint8_t {
    Refuse, // older than the fences in force: a replay
    Keep,   // the very fences already in force: loading them again would restart the fence engine
            // (and with it the per-breach shock count and the lost beacon), so only report back
    Load,
};
FenceUpdate fenceUpdate(uint16_t incomingVersion, const FenceSet &incoming, const FenceSet &current);

/// Replay guard for the Owner and Config messages. `seq` is the message's sequence number (0 = it
/// has none: a base older than 0.16 sent it) and `last` the newest number of that kind accepted so
/// far. A numbered message must be newer than the last one; one without a number is accepted only
/// until the first numbered one has arrived, because it cannot be told from a recording.
inline bool settingSeqAcceptable(uint32_t seq, uint32_t last)
{
    return seq ? seq > last : last == 0;
}

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
    uint8_t flags;      // kFlag* below
};

constexpr uint8_t kFlagCorrections = 0x01;    // corrections enabled
constexpr uint8_t kFlagShock = 0x02;          // shock enabled
constexpr uint8_t kFlagBeacon = 0x04;         // lost beacon on
constexpr uint8_t kFlagBeaconSilenced = 0x08; // lost beacon silenced by the owner
constexpr uint8_t kFlagNoFix = 0x10;          // no current GPS fix: the position is the last one known
constexpr uint8_t kFlagNumbered = 0x20;       // this firmware reads sequence numbers on Owner and Config

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
constexpr size_t kCommandLength = 2 + 1 + 1 + 2 + 4;

/// Peek at a message's type. Returns false if the buffer is not a GeoCollar v1 message.
bool messageType(const uint8_t *buf, size_t len, MsgType &type);

size_t encodeStatus(uint8_t *out, size_t cap, MsgType type, const StatusReport &s);
bool decodeStatus(const uint8_t *buf, size_t len, StatusReport &s);

/// Hello: [version][6][flags: bit0 has an admin key, bit1 has fences][battery %, 255 unknown]
constexpr size_t kHelloSize = 4;
size_t encodeHello(uint8_t *out, size_t cap, bool hasAdminKey, bool hasFences, uint8_t batteryPct);
/// Owner: [version][5][len][dog][len][owner][len][phone], then an optional sequence number (u32,
/// never 0). `seq` = 0 leaves it out when encoding and reports it absent when decoding.
size_t encodeOwner(uint8_t *out, size_t cap, const OwnerInfo &o, uint32_t seq = 0);
bool decodeOwner(const uint8_t *buf, size_t len, OwnerInfo &o, uint32_t &seq);

/// Report interval limits, seconds. 0 means the firmware default (GEOCOLLAR_STATUS_INTERVAL_MS).
constexpr uint16_t kMinReportS = 30;
constexpr uint16_t kMaxReportS = 3600;

/// Collar settings from the base: [version][7][report interval s, u16], then an optional sequence
/// number (u32, never 0; `seq` = 0 means none). Later fields may follow, so a longer message is
/// accepted.
struct CollarConfig {
    uint16_t reportS = 0;
};
constexpr size_t kConfigLength = 2 + 2;
size_t encodeConfig(uint8_t *out, size_t cap, const CollarConfig &c, uint32_t seq = 0);
bool decodeConfig(const uint8_t *buf, size_t len, CollarConfig &c, uint32_t &seq);

/// The collar's Meshtastic names for a dog's name: the long name is the dog's name (cut to fit on a
/// character boundary), the short name its initials (several words) or first letters (one word),
/// at most 4 bytes, ASCII letters upper-cased. Both NUL-terminated.
void meshNames(const char *dog, char *longName, size_t longCap, char *shortName, size_t shortCap);

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
