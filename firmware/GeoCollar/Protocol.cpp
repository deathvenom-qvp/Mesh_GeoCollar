// SPDX-License-Identifier: GPL-3.0-or-later

#include "Protocol.h"

#include <cstring>

namespace geocollar
{
namespace proto
{

namespace
{
struct Writer {
    uint8_t *out;
    size_t cap;
    size_t pos = 0;
    bool ok = true;
    void u8(uint8_t v)
    {
        if (pos + 1 > cap) {
            ok = false;
            return;
        }
        out[pos++] = v;
    }
    void u16(uint16_t v)
    {
        u8(v & 0xFF);
        u8(v >> 8);
    }
    void u32(uint32_t v)
    {
        u16(v & 0xFFFF);
        u16(v >> 16);
    }
    void bytes(const uint8_t *b, size_t n)
    {
        if (pos + n > cap) {
            ok = false;
            return;
        }
        memcpy(out + pos, b, n);
        pos += n;
    }
    size_t done() const { return ok ? pos : 0; }
};

struct Reader {
    const uint8_t *buf;
    size_t len;
    size_t pos = 0;
    bool ok = true;
    uint8_t u8()
    {
        if (pos + 1 > len) {
            ok = false;
            return 0;
        }
        return buf[pos++];
    }
    uint16_t u16()
    {
        uint16_t lo = u8();
        return lo | (static_cast<uint16_t>(u8()) << 8);
    }
    uint32_t u32()
    {
        uint32_t lo = u16();
        return lo | (static_cast<uint32_t>(u16()) << 16);
    }
};

bool header(Reader &r, MsgType expect)
{
    const uint8_t v = r.u8();
    const uint8_t t = r.u8();
    return r.ok && v == kVersion && t == static_cast<uint8_t>(expect);
}
} // namespace

bool messageType(const uint8_t *buf, size_t len, MsgType &type)
{
    if (len < 2 || buf[0] != kVersion || buf[1] < 1 || buf[1] > 4)
        return false;
    type = static_cast<MsgType>(buf[1]);
    return true;
}

size_t encodeStatus(uint8_t *out, size_t cap, MsgType type, const StatusReport &s)
{
    Writer w{out, cap};
    w.u8(kVersion);
    w.u8(static_cast<uint8_t>(type));
    w.u16(s.fenceVersion);
    w.u8(s.state);
    w.u8(s.batteryPct);
    w.u32(static_cast<uint32_t>(s.latI));
    w.u32(static_cast<uint32_t>(s.lonI));
    w.u16(s.pdopX100);
    w.u8(s.sats);
    w.u16(static_cast<uint16_t>(s.clearanceM));
    w.u8(s.flags);
    return w.done();
}

bool decodeStatus(const uint8_t *buf, size_t len, StatusReport &s)
{
    Reader r{buf, len};
    const uint8_t v = r.u8();
    const uint8_t t = r.u8();
    if (!r.ok || v != kVersion || (t != static_cast<uint8_t>(MsgType::Status) && t != static_cast<uint8_t>(MsgType::Breach)))
        return false;
    s.fenceVersion = r.u16();
    s.state = r.u8();
    s.batteryPct = r.u8();
    s.latI = static_cast<int32_t>(r.u32());
    s.lonI = static_cast<int32_t>(r.u32());
    s.pdopX100 = r.u16();
    s.sats = r.u8();
    s.clearanceM = static_cast<int16_t>(r.u16());
    s.flags = r.u8();
    return r.ok;
}

size_t encodeCommand(uint8_t *out, size_t cap, const Command &c)
{
    Writer w{out, cap};
    w.u8(kVersion);
    w.u8(static_cast<uint8_t>(MsgType::Command));
    w.u8(static_cast<uint8_t>(c.action));
    w.u8(c.intensity);
    w.u16(c.durationMs);
    return w.done();
}

bool decodeCommand(const uint8_t *buf, size_t len, Command &c)
{
    Reader r{buf, len};
    if (!header(r, MsgType::Command))
        return false;
    c.action = static_cast<CommandAction>(r.u8());
    c.intensity = r.u8();
    c.durationMs = r.u16();
    return r.ok;
}

size_t encodeFenceChunk(uint8_t *out, size_t cap, const FenceChunk &c)
{
    if (c.length > kMaxChunkData)
        return 0;
    Writer w{out, cap};
    w.u8(kVersion);
    w.u8(static_cast<uint8_t>(MsgType::FenceChunk));
    w.u16(c.fenceVersion);
    w.u8(c.index);
    w.u8(c.count);
    w.bytes(c.data, c.length);
    return w.done();
}

bool decodeFenceChunk(const uint8_t *buf, size_t len, FenceChunk &c)
{
    Reader r{buf, len};
    if (!header(r, MsgType::FenceChunk))
        return false;
    c.fenceVersion = r.u16();
    c.index = r.u8();
    c.count = r.u8();
    if (!r.ok || c.count == 0 || c.count > 8 || c.index >= c.count)
        return false;
    c.data = buf + r.pos;
    c.length = len - r.pos;
    return c.length <= kMaxChunkData;
}

// Fence set blob: u8 zoneCount, u8 beepPulseS, u8 vibratePulseS, then per zone: u8 kind, u8 vertexCount, u8 dangerM, u8 warningM,
// u8 attentionM, u8 reserved (0), then vertexCount pairs of (i32 latI, i32 lonI).
size_t encodeFenceSet(uint8_t *out, size_t cap, const FenceSet &f)
{
    Writer w{out, cap};
    w.u8(f.zoneCount);
    w.u8(f.beepPulseS);
    w.u8(f.vibratePulseS);
    for (size_t i = 0; i < f.zoneCount && i < kMaxZones; i++) {
        const Zone &z = f.zones[i];
        w.u8(static_cast<uint8_t>(z.kind));
        w.u8(z.vertexCount);
        w.u8(z.dangerM);
        w.u8(z.warningM);
        w.u8(z.attentionM);
        w.u8(0);
        for (size_t v = 0; v < z.vertexCount && v < kMaxVertices; v++) {
            w.u32(static_cast<uint32_t>(z.latI[v]));
            w.u32(static_cast<uint32_t>(z.lonI[v]));
        }
    }
    return w.done();
}

bool decodeFenceSet(const uint8_t *buf, size_t len, uint16_t version, FenceSet &f)
{
    Reader r{buf, len};
    FenceSet tmp;
    tmp.version = version;
    tmp.zoneCount = r.u8();
    tmp.beepPulseS = r.u8();
    tmp.vibratePulseS = r.u8();
    if (!r.ok || tmp.zoneCount > kMaxZones || tmp.beepPulseS > kMaxPulseS || tmp.vibratePulseS > kMaxPulseS)
        return false;
    for (size_t i = 0; i < tmp.zoneCount; i++) {
        Zone &z = tmp.zones[i];
        const uint8_t kind = r.u8();
        if (kind > 1)
            return false;
        z.kind = static_cast<ZoneKind>(kind);
        z.vertexCount = r.u8();
        z.dangerM = r.u8();
        z.warningM = r.u8();
        z.attentionM = r.u8();
        r.u8(); // reserved
        if (!r.ok || z.vertexCount < 3 || z.vertexCount > kMaxVertices)
            return false;
        for (size_t v = 0; v < z.vertexCount; v++) {
            z.latI[v] = static_cast<int32_t>(r.u32());
            z.lonI[v] = static_cast<int32_t>(r.u32());
        }
    }
    if (!r.ok || r.pos != len)
        return false;
    f = tmp;
    return true;
}

void FenceAssembler::reset(uint16_t version, uint8_t count)
{
    version_ = version;
    count_ = count;
    have_ = 0;
    length_ = 0;
    active_ = true;
    for (auto &l : chunkLen_)
        l = 0;
}

bool FenceAssembler::add(const FenceChunk &c)
{
    if (!active_ || c.fenceVersion != version_ || c.count != count_)
        reset(c.fenceVersion, c.count);

    // Every chunk except the last is exactly kMaxChunkData long, so offsets are fixed.
    const size_t offset = static_cast<size_t>(c.index) * kMaxChunkData;
    if (offset + c.length > kMaxFenceBlob)
        return false;
    if (c.index + 1 < c.count && c.length != kMaxChunkData)
        return false;

    memcpy(buf_ + offset, c.data, c.length);
    chunkLen_[c.index] = c.length;
    have_ |= static_cast<uint8_t>(1u << c.index);

    const uint8_t all = static_cast<uint8_t>((1u << count_) - 1u);
    if (have_ != all)
        return false;

    length_ = 0;
    for (uint8_t i = 0; i < count_; i++)
        length_ += chunkLen_[i];
    active_ = false;
    return true;
}

} // namespace proto
} // namespace geocollar
