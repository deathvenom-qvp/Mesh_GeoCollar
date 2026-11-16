// SPDX-License-Identifier: GPL-3.0-or-later
//
// The companion app's side door into the collar. A phone connects to the collar over Meshtastic's
// own Bluetooth service and exchanges GeoCollar messages with it as a local client. Being paired
// over Bluetooth is not enough to command the collar: every request must be signed with a key the
// base station handed to that phone (in a QR code) and to the collar (over the mesh).
//
// No hardware in here, so the PC tests and the app's own tests run this code. See docs/protocol.md.

#pragma once

#include "Protocol.h"

#include <cstddef>
#include <cstdint>

namespace geocollar
{
namespace proto
{

constexpr size_t kMaxAppKeys = 4;
constexpr size_t kAppKeyLen = 16;
constexpr size_t kAppNonceLen = 8;
constexpr size_t kAppTagLen = 8;
constexpr size_t kAppSessionKeyLen = 32;

/// One authorized phone: a number the base gave it (never 0) and the secret they share.
struct AppKey {
    uint32_t id = 0;
    uint8_t key[kAppKeyLen] = {};
};

/// Every phone the base has authorized for this collar. Plain data: it is written to flash as is.
struct AppKeySet {
    uint8_t count = 0;
    AppKey keys[kMaxAppKeys];
    const AppKey *find(uint32_t id) const;
};

// AppKeys (base -> collar): [version][8][seq u32][count u8][count x (id u32, key 16 bytes)].
// The whole list every time, so leaving a phone out revokes it. `seq` is never 0.
constexpr size_t kAppKeysHeader = 2 + 4 + 1;
constexpr size_t kAppKeysMax = kAppKeysHeader + kMaxAppKeys * (4 + kAppKeyLen);
size_t encodeAppKeys(uint8_t *out, size_t cap, const AppKeySet &keys, uint32_t seq);
bool decodeAppKeys(const uint8_t *buf, size_t len, AppKeySet &keys, uint32_t &seq);

// AppKeysAck (collar -> base): [version][9][seq u32][count u8]: the list numbered `seq` is in force.
constexpr size_t kAppKeysAckLength = 2 + 4 + 1;
size_t encodeAppKeysAck(uint8_t *out, size_t cap, uint32_t seq, uint8_t count);
bool decodeAppKeysAck(const uint8_t *buf, size_t len, uint32_t &seq, uint8_t &count);

// Between the app and the collar:
//   AppHello     (app -> collar)  [version][16][key id u32][app nonce 8]
//   AppChallenge (collar -> app)  [version][17][key id u32][collar nonce 8][tag 8]
//   AppRequest   (app -> collar)  [version][18][key id u32][counter u32][action][intensity][duration ms u16][tag 8]
//   AppStatus    (collar -> app)  [version][19][key id u32][counter u32][result][status, 18 bytes][fix age s u16][tag 8]
//   AppError     (collar -> app)  [version][20][key id u32][code]
constexpr size_t kAppHelloLength = 2 + 4 + kAppNonceLen;
constexpr size_t kAppChallengeLength = 2 + 4 + kAppNonceLen + kAppTagLen;
constexpr size_t kAppRequestLength = 2 + 4 + 4 + 1 + 1 + 2 + kAppTagLen;
constexpr size_t kAppStatusLength = 2 + 4 + 4 + 1 + (kStatusLength - 2) + 2 + kAppTagLen;
constexpr size_t kAppErrorLength = 2 + 4 + 1;
constexpr uint16_t kAppNoFixAge = 0xFFFF; // the collar has had no fix since it started

enum class AppResult : uint8_t {
    Done = 0,
    NotAllowed = 1, // an action phones may not ask for (shock, forcing or clearing lost mode)
};

enum class AppErrorCode : uint8_t {
    UnknownKey = 1, // the base has not authorized this phone, or has removed it
    NoSession = 2,  // say hello again: no session, a wrong signature or a counter already used
};

struct AppRequest {
    uint32_t keyId = 0;
    uint32_t counter = 0;
    CommandAction action = CommandAction::RequestStatus;
    uint8_t intensity = 0;
    uint16_t durationMs = 0;
};

/// What a phone may ask for: a report, silencing the lost beacon, a beep or a vibration, and
/// switching the fence's corrections off and on. Never shock.
bool appActionAllowed(CommandAction a);

/// tag = HMAC-SHA256(key, "GCA1" | key id | app nonce | collar nonce), first 8 bytes.
void appChallengeTag(const AppKey &k, const uint8_t appNonce[kAppNonceLen], const uint8_t collarNonce[kAppNonceLen],
                     uint8_t tag[kAppTagLen]);
/// session key = HMAC-SHA256(key, "GCS1" | key id | app nonce | collar nonce).
void appSessionKey(const AppKey &k, const uint8_t appNonce[kAppNonceLen], const uint8_t collarNonce[kAppNonceLen],
                   uint8_t out[kAppSessionKeyLen]);
/// The tag on a request or a status: HMAC-SHA256(session key, the message before its tag), first 8 bytes.
void appMessageTag(const uint8_t sessionKey[kAppSessionKeyLen], const uint8_t *msg, size_t len, uint8_t tag[kAppTagLen]);

// The app's half, used by the tests (the app itself is JavaScript, with the same golden bytes).
size_t encodeAppHello(uint8_t *out, size_t cap, uint32_t keyId, const uint8_t appNonce[kAppNonceLen]);
size_t encodeAppRequest(uint8_t *out, size_t cap, const AppRequest &r, const uint8_t sessionKey[kAppSessionKeyLen]);

/// The collar's half: one session at a time, kept in RAM. A hello starts a new one and ends the
/// one before it. Each session has fresh nonces from both sides, so a recording of an earlier
/// session is worthless, and within a session the counter must grow with every request.
class AppLink
{
  public:
    enum class Outcome : uint8_t {
        Ignore,  // not an app message
        Reply,   // `reply` holds a challenge or an error to send back
        Request, // `request` is authentic: act on it, then answer with status()
    };

    /// A message from a local client. `collarNonce` must be fresh random bytes on every call.
    Outcome handle(const uint8_t *buf, size_t len, const AppKeySet &keys, const uint8_t collarNonce[kAppNonceLen],
                   uint8_t *reply, size_t cap, size_t &replyLen, AppRequest &request);

    /// The signed answer to `request`. fixAgeS: how long ago the position was taken (kAppNoFixAge: never).
    size_t status(uint8_t *out, size_t cap, const AppRequest &request, AppResult result, const StatusReport &s,
                  uint16_t fixAgeS) const;

    /// The key list changed: a session whose key is gone or different ends.
    void keysChanged(const AppKeySet &keys);
    bool active() const { return active_; }

  private:
    bool active_ = false;
    AppKey key_;
    uint8_t sessionKey_[kAppSessionKeyLen] = {};
    uint32_t lastCounter_ = 0;
};

} // namespace proto
} // namespace geocollar
