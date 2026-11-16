// SPDX-License-Identifier: GPL-3.0-or-later

#include "AppLink.h"

#include "Sha256.h"

#include <cstring>

namespace geocollar
{
namespace proto
{

namespace
{
void put32(uint8_t *p, uint32_t v)
{
    p[0] = static_cast<uint8_t>(v);
    p[1] = static_cast<uint8_t>(v >> 8);
    p[2] = static_cast<uint8_t>(v >> 16);
    p[3] = static_cast<uint8_t>(v >> 24);
}

uint32_t get32(const uint8_t *p)
{
    return p[0] | (static_cast<uint32_t>(p[1]) << 8) | (static_cast<uint32_t>(p[2]) << 16) | (static_cast<uint32_t>(p[3]) << 24);
}

bool isType(const uint8_t *buf, size_t len, MsgType t, size_t want)
{
    return len == want && buf[0] == kVersion && buf[1] == static_cast<uint8_t>(t);
}

void head(uint8_t *out, MsgType t, uint32_t keyId)
{
    out[0] = kVersion;
    out[1] = static_cast<uint8_t>(t);
    put32(out + 2, keyId);
}

/// HMAC over "<label>" | key id | app nonce | collar nonce with the phone's key.
void handshakeMac(const char *label, const AppKey &k, const uint8_t *appNonce, const uint8_t *collarNonce,
                  uint8_t out[hash::kSha256Len])
{
    uint8_t msg[4 + 4 + 2 * kAppNonceLen];
    memcpy(msg, label, 4);
    put32(msg + 4, k.id);
    memcpy(msg + 8, appNonce, kAppNonceLen);
    memcpy(msg + 8 + kAppNonceLen, collarNonce, kAppNonceLen);
    hash::hmacSha256(k.key, kAppKeyLen, msg, sizeof(msg), out);
}

size_t errorReply(uint8_t *out, size_t cap, uint32_t keyId, AppErrorCode code)
{
    if (cap < kAppErrorLength)
        return 0;
    head(out, MsgType::AppError, keyId);
    out[6] = static_cast<uint8_t>(code);
    return kAppErrorLength;
}
} // namespace

const AppKey *AppKeySet::find(uint32_t id) const
{
    for (uint8_t i = 0; i < count && i < kMaxAppKeys; i++)
        if (keys[i].id == id)
            return &keys[i];
    return nullptr;
}

size_t encodeAppKeys(uint8_t *out, size_t cap, const AppKeySet &keys, uint32_t seq)
{
    const size_t len = kAppKeysHeader + keys.count * (4 + kAppKeyLen);
    if (keys.count > kMaxAppKeys || cap < len || seq == 0)
        return 0;
    out[0] = kVersion;
    out[1] = static_cast<uint8_t>(MsgType::AppKeys);
    put32(out + 2, seq);
    out[6] = keys.count;
    uint8_t *p = out + kAppKeysHeader;
    for (uint8_t i = 0; i < keys.count; i++, p += 4 + kAppKeyLen) {
        put32(p, keys.keys[i].id);
        memcpy(p + 4, keys.keys[i].key, kAppKeyLen);
    }
    return len;
}

bool decodeAppKeys(const uint8_t *buf, size_t len, AppKeySet &keys, uint32_t &seq)
{
    if (len < kAppKeysHeader || buf[0] != kVersion || buf[1] != static_cast<uint8_t>(MsgType::AppKeys))
        return false;
    const uint8_t count = buf[6];
    if (count > kMaxAppKeys || len != kAppKeysHeader + count * (4 + kAppKeyLen))
        return false;
    seq = get32(buf + 2);
    if (seq == 0)
        return false;
    AppKeySet out;
    out.count = count;
    const uint8_t *p = buf + kAppKeysHeader;
    for (uint8_t i = 0; i < count; i++, p += 4 + kAppKeyLen) {
        out.keys[i].id = get32(p);
        memcpy(out.keys[i].key, p + 4, kAppKeyLen);
        if (out.keys[i].id == 0)
            return false;
        for (uint8_t j = 0; j < i; j++)
            if (out.keys[j].id == out.keys[i].id)
                return false; // one key per phone
    }
    keys = out;
    return true;
}

size_t encodeAppKeysAck(uint8_t *out, size_t cap, uint32_t seq, uint8_t count)
{
    if (cap < kAppKeysAckLength)
        return 0;
    out[0] = kVersion;
    out[1] = static_cast<uint8_t>(MsgType::AppKeysAck);
    put32(out + 2, seq);
    out[6] = count;
    return kAppKeysAckLength;
}

bool decodeAppKeysAck(const uint8_t *buf, size_t len, uint32_t &seq, uint8_t &count)
{
    if (!isType(buf, len, MsgType::AppKeysAck, kAppKeysAckLength))
        return false;
    seq = get32(buf + 2);
    count = buf[6];
    return true;
}

bool appActionAllowed(CommandAction a)
{
    switch (a) {
    case CommandAction::RequestStatus:
    case CommandAction::SilenceLost:
    case CommandAction::Beep:
    case CommandAction::Vibrate:
    case CommandAction::CorrectionsOff:
    case CommandAction::CorrectionsOn:
        return true;
    default:
        return false;
    }
}

void appChallengeTag(const AppKey &k, const uint8_t appNonce[kAppNonceLen], const uint8_t collarNonce[kAppNonceLen],
                     uint8_t tag[kAppTagLen])
{
    uint8_t mac[hash::kSha256Len];
    handshakeMac("GCA1", k, appNonce, collarNonce, mac);
    memcpy(tag, mac, kAppTagLen);
}

void appSessionKey(const AppKey &k, const uint8_t appNonce[kAppNonceLen], const uint8_t collarNonce[kAppNonceLen],
                   uint8_t out[kAppSessionKeyLen])
{
    handshakeMac("GCS1", k, appNonce, collarNonce, out);
}

void appMessageTag(const uint8_t sessionKey[kAppSessionKeyLen], const uint8_t *msg, size_t len, uint8_t tag[kAppTagLen])
{
    uint8_t mac[hash::kSha256Len];
    hash::hmacSha256(sessionKey, kAppSessionKeyLen, msg, len, mac);
    memcpy(tag, mac, kAppTagLen);
}

size_t encodeAppHello(uint8_t *out, size_t cap, uint32_t keyId, const uint8_t appNonce[kAppNonceLen])
{
    if (cap < kAppHelloLength)
        return 0;
    head(out, MsgType::AppHello, keyId);
    memcpy(out + 6, appNonce, kAppNonceLen);
    return kAppHelloLength;
}

size_t encodeAppRequest(uint8_t *out, size_t cap, const AppRequest &r, const uint8_t sessionKey[kAppSessionKeyLen])
{
    if (cap < kAppRequestLength)
        return 0;
    head(out, MsgType::AppRequest, r.keyId);
    put32(out + 6, r.counter);
    out[10] = static_cast<uint8_t>(r.action);
    out[11] = r.intensity;
    out[12] = static_cast<uint8_t>(r.durationMs);
    out[13] = static_cast<uint8_t>(r.durationMs >> 8);
    appMessageTag(sessionKey, out, kAppRequestLength - kAppTagLen, out + kAppRequestLength - kAppTagLen);
    return kAppRequestLength;
}

AppLink::Outcome AppLink::handle(const uint8_t *buf, size_t len, const AppKeySet &keys,
                                 const uint8_t collarNonce[kAppNonceLen], uint8_t *reply, size_t cap, size_t &replyLen,
                                 AppRequest &request)
{
    replyLen = 0;
    if (len < 6 || buf[0] != kVersion)
        return Outcome::Ignore;
    const uint32_t keyId = get32(buf + 2);

    if (isType(buf, len, MsgType::AppHello, kAppHelloLength)) {
        const AppKey *k = keys.find(keyId);
        if (!k) {
            replyLen = errorReply(reply, cap, keyId, AppErrorCode::UnknownKey);
            return replyLen ? Outcome::Reply : Outcome::Ignore;
        }
        if (cap < kAppChallengeLength)
            return Outcome::Ignore;
        const uint8_t *appNonce = buf + 6;
        key_ = *k;
        appSessionKey(*k, appNonce, collarNonce, sessionKey_);
        lastCounter_ = 0;
        active_ = true;
        head(reply, MsgType::AppChallenge, keyId);
        memcpy(reply + 6, collarNonce, kAppNonceLen);
        appChallengeTag(*k, appNonce, collarNonce, reply + 6 + kAppNonceLen);
        replyLen = kAppChallengeLength;
        return Outcome::Reply;
    }

    if (isType(buf, len, MsgType::AppRequest, kAppRequestLength)) {
        const AppErrorCode why = keys.find(keyId) ? AppErrorCode::NoSession : AppErrorCode::UnknownKey;
        uint8_t tag[kAppTagLen];
        bool ok = active_ && keyId == key_.id;
        if (ok) {
            appMessageTag(sessionKey_, buf, kAppRequestLength - kAppTagLen, tag);
            ok = hash::sameBytes(tag, buf + kAppRequestLength - kAppTagLen, kAppTagLen);
        }
        const uint32_t counter = get32(buf + 6);
        // The signature first: a counter is only believed, and only used up, on a signed request.
        if (!ok || counter <= lastCounter_) {
            replyLen = errorReply(reply, cap, keyId, why);
            return replyLen ? Outcome::Reply : Outcome::Ignore;
        }
        lastCounter_ = counter;
        request.keyId = keyId;
        request.counter = counter;
        request.action = static_cast<CommandAction>(buf[10]);
        request.intensity = buf[11];
        request.durationMs = static_cast<uint16_t>(buf[12] | (buf[13] << 8));
        return Outcome::Request;
    }
    return Outcome::Ignore;
}

size_t AppLink::status(uint8_t *out, size_t cap, const AppRequest &request, AppResult result, const StatusReport &s,
                       uint16_t fixAgeS) const
{
    if (!active_ || cap < kAppStatusLength)
        return 0;
    head(out, MsgType::AppStatus, request.keyId);
    put32(out + 6, request.counter);
    out[10] = static_cast<uint8_t>(result);
    uint8_t body[kStatusLength];
    if (encodeStatus(body, sizeof(body), MsgType::Status, s) != kStatusLength)
        return 0;
    memcpy(out + 11, body + 2, kStatusLength - 2); // the report without its own header
    uint8_t *p = out + 11 + (kStatusLength - 2);
    p[0] = static_cast<uint8_t>(fixAgeS);
    p[1] = static_cast<uint8_t>(fixAgeS >> 8);
    appMessageTag(sessionKey_, out, kAppStatusLength - kAppTagLen, out + kAppStatusLength - kAppTagLen);
    return kAppStatusLength;
}

void AppLink::keysChanged(const AppKeySet &keys)
{
    if (!active_)
        return;
    const AppKey *k = keys.find(key_.id);
    if (!k || !hash::sameBytes(k->key, key_.key, kAppKeyLen)) {
        active_ = false;
        memset(sessionKey_, 0, sizeof(sessionKey_));
    }
}

} // namespace proto
} // namespace geocollar
