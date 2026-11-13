// SPDX-License-Identifier: GPL-3.0-or-later
//
// Host tests for the parts of GeoCollar that do not need hardware: the CaiXianLin frame builder,
// the fence engine and the wire protocol. Build and run with `make -C firmware/test`.

#include "../GeoCollar/CaiXianLin.h"
#include "../GeoCollar/Fence.h"
#include "../GeoCollar/Protocol.h"

#include <cmath>
#include <cstdio>
#include <cstring>
#include <string>

using namespace geocollar;

static int failures = 0;
static int checks = 0;

#define CHECK(cond)                                                                                                    \
    do {                                                                                                               \
        checks++;                                                                                                      \
        if (!(cond)) {                                                                                                 \
            failures++;                                                                                                \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);                                                \
        }                                                                                                              \
    } while (0)

static std::string hex(const uint8_t *b, size_t n)
{
    static const char *d = "0123456789abcdef";
    std::string s;
    for (size_t i = 0; i < n; i++) {
        s += d[b[i] >> 4];
        s += d[b[i] & 15];
    }
    return s;
}

// --------------------------------------------------------------------------------------------
// CaiXianLin
// --------------------------------------------------------------------------------------------

static void testCaiXianLin()
{
    using namespace caixianlin;
    // Payload layout [id:16][channel:4][action:4][intensity:8].
    CHECK(payload(0x1234, 1, CollarAction::Vibrate, 50) == 0x12341232u);
    CHECK(payload(0x1234, 0, CollarAction::Shock, 200) == 0x12340163u); // clamped to 99
    CHECK(payload(0x1234, 2, CollarAction::Beep, 40) == 0x12342300u);   // beep forces 0
    CHECK(checksum(0x12341232u) == static_cast<uint8_t>(0x12 + 0x34 + 0x12 + 0x32));

    Pulse f[kFrameLength];
    CHECK(!buildFrame(f, 1, 3, CollarAction::Beep, 0)); // channel 3 is invalid
    CHECK(buildFrame(f, 0x1234, 1, CollarAction::Vibrate, 50));
    CHECK(f[0].highUs == 1400 && f[0].lowUs == 750);

    // Decode the bits back out of the pulses and compare.
    uint64_t bits = 0;
    for (size_t i = 1; i < kFrameLength; i++) {
        CHECK(f[i].highUs + f[i].lowUs == 1000);
        bits = (bits << 1) | (f[i].highUs == 750 ? 1u : 0u);
    }
    const uint32_t p = 0x12341232u;
    CHECK(bits == ((((uint64_t)p << 8) | checksum(p)) << 3));
    CHECK((bits & 0x7) == 0); // three-bit zero postamble
    CHECK(kFrameUs == 45150);
}

// --------------------------------------------------------------------------------------------
// Fence geometry
// --------------------------------------------------------------------------------------------

// A square keep-in zone about 222 m on a side, near 40.0N 75.0W.
static Zone square(ZoneKind kind = ZoneKind::KeepIn, uint16_t margin = 10)
{
    Zone z;
    z.kind = kind;
    z.warnMarginM = margin;
    z.vertexCount = 4;
    const int32_t lat0 = 400000000, lon0 = -750000000, d = 20000; // 0.002 degrees
    z.latI[0] = lat0;
    z.lonI[0] = lon0;
    z.latI[1] = lat0;
    z.lonI[1] = lon0 + d;
    z.latI[2] = lat0 + d;
    z.lonI[2] = lon0 + d;
    z.latI[3] = lat0 + d;
    z.lonI[3] = lon0;
    return z;
}

// Offsets in metres from the square's south-west corner.
static int32_t latAt(double northM)
{
    return 400000000 + (int32_t)std::lround(northM / 111195.08 * 1e7);
}
static int32_t lonAt(double eastM)
{
    return -750000000 + (int32_t)std::lround(eastM / (111195.08 * std::cos(40.0 * M_PI / 180)) * 1e7);
}

static void testGeometry()
{
    const Zone z = square();
    // Centre: about 111 m from every side.
    const float centre = signedDistanceM(z, latAt(111.2), lonAt(85.2));
    CHECK(centre > 80 && centre < 90);
    // 5 m inside the south edge.
    const float nearSouth = signedDistanceM(z, latAt(5), lonAt(85));
    CHECK(std::fabs(nearSouth - 5.0f) < 0.5f);
    // 7 m outside the south edge: negative.
    const float outside = signedDistanceM(z, latAt(-7), lonAt(85));
    CHECK(std::fabs(outside + 7.0f) < 0.5f);

    // A keep-out zone flips the sign.
    const Zone pond = square(ZoneKind::KeepOut);
    CHECK(signedDistanceM(pond, latAt(50), lonAt(50)) < 0);
    CHECK(signedDistanceM(pond, latAt(-30), lonAt(50)) > 0);

    // Clearance takes the tightest zone.
    FenceSet f;
    f.zoneCount = 1;
    f.zones[0] = z;
    CHECK(clearanceM(f, latAt(5), lonAt(85)) < 6);
    FenceSet none;
    CHECK(clearanceM(none, latAt(5), lonAt(85)) > 1e8f);
}

// --------------------------------------------------------------------------------------------
// State machine
// --------------------------------------------------------------------------------------------

static Fix fixAt(double northM, double eastM, uint32_t t, uint16_t pdop = 150, uint8_t sats = 9)
{
    return Fix{latAt(northM), lonAt(eastM), pdop, sats, t};
}

static void testStateMachine()
{
    FenceEngine e;
    // No fences: nothing happens.
    CHECK(e.onFix(fixAt(-50, 50, 0)).correction == Correction::None);
    CHECK(e.state() == CollarState::NoFence);

    FenceSet f;
    f.version = 7;
    f.zoneCount = 1;
    f.zones[0] = square(ZoneKind::KeepIn, 10);
    e.setFences(f);
    CHECK(e.state() == CollarState::Inside);

    uint32_t t = 1000;
    // Well inside.
    Decision d = e.onFix(fixAt(100, 100, t += 1000));
    CHECK(d.correction == Correction::None && e.state() == CollarState::Inside);

    // 8 m from the line, margin 10: beep.
    d = e.onFix(fixAt(8, 100, t += 1000));
    CHECK(d.correction == Correction::Beep && e.state() == CollarState::Warning && d.sendStatus);

    // 3 m from the line (under half the margin): vibrate.
    d = e.onFix(fixAt(3, 100, t += 1000));
    CHECK(d.correction == Correction::Vibrate);

    // A poor fix outside is ignored entirely.
    d = e.onFix(fixAt(-20, 100, t += 1000, 900, 9));
    CHECK(d.fixRejected && d.correction == Correction::None && e.state() == CollarState::Warning);
    d = e.onFix(fixAt(-20, 100, t += 1000, 150, 3));
    CHECK(d.fixRejected);

    // Outside, shock disabled by default: vibrate only.
    d = e.onFix(fixAt(-5, 100, t += 1000));
    CHECK(e.state() == CollarState::Outside && d.correction == Correction::Vibrate);
    d = e.onFix(fixAt(-8, 100, t += 1000));
    CHECK(d.correction == Correction::Vibrate);
    // Third consecutive outside fix: lost. Breach sent, corrections stop.
    d = e.onFix(fixAt(-12, 100, t += 1000));
    CHECK(e.state() == CollarState::Lost && d.sendBreach && d.correction == Correction::None);
    d = e.onFix(fixAt(-40, 100, t += 1000));
    CHECK(d.correction == Correction::None && !d.sendBreach);

    // Coming back: needs rearmFixes good fixes at least rearmMarginM inside.
    d = e.onFix(fixAt(2, 100, t += 1000)); // inside but not by 5 m
    CHECK(e.state() == CollarState::Lost);
    e.onFix(fixAt(20, 100, t += 1000));
    e.onFix(fixAt(20, 100, t += 1000));
    d = e.onFix(fixAt(20, 100, t += 1000));
    CHECK(e.state() == CollarState::Inside && d.sendStatus);
}

static void testShockCaps()
{
    FenceEngine e;
    FenceSet f;
    f.zoneCount = 1;
    f.zones[0] = square();
    e.setFences(f);
    Settings &s = e.settings();
    s.shockEnabled = true;
    s.shockLevel = 60;
    s.maxShockLevel = 30;
    s.lostConfirmFixes = 10; // keep it in Outside for this test
    s.maxShocksPerBreach = 2;
    s.maxShocksPerHour = 3;

    uint32_t t = 0;
    Decision d = e.onFix(fixAt(-5, 100, t += 1000));
    CHECK(d.correction == Correction::Shock && d.intensity == 30); // capped level
    d = e.onFix(fixAt(-5, 100, t += 1000));
    CHECK(d.correction == Correction::Shock);
    d = e.onFix(fixAt(-5, 100, t += 1000));
    CHECK(d.correction == Correction::Vibrate); // per-breach cap reached

    // Back inside resets the per-breach count, but the hourly cap (3) still applies.
    e.onFix(fixAt(100, 100, t += 1000));
    d = e.onFix(fixAt(-5, 100, t += 1000));
    CHECK(d.correction == Correction::Shock);
    e.onFix(fixAt(100, 100, t += 1000));
    d = e.onFix(fixAt(-5, 100, t += 1000));
    CHECK(d.correction == Correction::Vibrate); // 3 shocks in the last hour

    // An hour later shocks are allowed again.
    e.onFix(fixAt(100, 100, t += 3600000));
    d = e.onFix(fixAt(-5, 100, t += 1000));
    CHECK(d.correction == Correction::Shock);

    // Corrections off: nothing at all.
    s.correctionsEnabled = false;
    e.onFix(fixAt(100, 100, t += 1000));
    d = e.onFix(fixAt(-5, 100, t += 1000));
    CHECK(d.correction == Correction::None);
}

// --------------------------------------------------------------------------------------------
// Protocol
// --------------------------------------------------------------------------------------------

static void testProtocol()
{
    using namespace proto;
    uint8_t buf[256];

    // Golden vectors, shared with base/tests/test_protocol.py.
    Command c{CommandAction::Vibrate, 40, 1500};
    size_t n = encodeCommand(buf, sizeof(buf), c);
    CHECK(hex(buf, n) == "01040228dc05");
    Command c2{};
    CHECK(decodeCommand(buf, n, c2) && c2.action == CommandAction::Vibrate && c2.intensity == 40 && c2.durationMs == 1500);

    StatusReport s{3, 4, 87, 400001234, -750004321, 180, 9, -12, 1};
    n = encodeStatus(buf, sizeof(buf), MsgType::Breach, s);
    CHECK(n == kStatusLength);
    CHECK(hex(buf, n) == "010303000457d288d7179fd74bd3b40009f4ff01");
    StatusReport s2{};
    CHECK(decodeStatus(buf, n, s2) && s2.latI == 400001234 && s2.lonI == -750004321 && s2.clearanceM == -12 &&
          s2.state == 4);
    MsgType type;
    CHECK(messageType(buf, n, type) && type == MsgType::Breach);

    // Fence set round trip, chunked and reassembled out of order.
    FenceSet f;
    f.zoneCount = 2;
    f.zones[0] = square(ZoneKind::KeepIn, 12);
    f.zones[1] = square(ZoneKind::KeepOut, 5);
    f.zones[1].vertexCount = 32; // make it big enough to need several chunks
    for (int i = 4; i < 32; i++) {
        f.zones[1].latI[i] = 400000000 + i * 100;
        f.zones[1].lonI[i] = -750000000 + i * 100;
    }
    uint8_t blob[kMaxFenceBlob];
    const size_t blobLen = encodeFenceSet(blob, sizeof(blob), f);
    CHECK(blobLen == 1 + (4 + 4 * 8) + (4 + 32 * 8));

    const uint8_t count = (blobLen + kMaxChunkData - 1) / kMaxChunkData;
    CHECK(count == 2);
    FenceAssembler a;
    bool complete = false;
    for (int i = count - 1; i >= 0; i--) {
        FenceChunk ch{9, (uint8_t)i, count, blob + i * kMaxChunkData,
                      (size_t)(i + 1 < count ? kMaxChunkData : blobLen - i * kMaxChunkData)};
        n = encodeFenceChunk(buf, sizeof(buf), ch);
        FenceChunk back{};
        CHECK(decodeFenceChunk(buf, n, back));
        complete = a.add(back);
    }
    CHECK(complete && a.version() == 9 && a.length() == blobLen);
    FenceSet g;
    CHECK(decodeFenceSet(a.data(), a.length(), a.version(), g));
    CHECK(g.version == 9 && g.zoneCount == 2 && g.zones[0].warnMarginM == 12 && g.zones[1].kind == ZoneKind::KeepOut &&
          g.zones[1].vertexCount == 32 && g.zones[1].lonI[31] == -750000000 + 3100);

    // Golden fence blob for one small zone, shared with the Python tests.
    FenceSet one;
    one.zoneCount = 1;
    one.zones[0] = square(ZoneKind::KeepIn, 10);
    n = encodeFenceSet(buf, sizeof(buf), one);
    CHECK(hex(buf, n) == "01" "00" "04" "0a00"  // 1 zone: keep-in, 4 vertices, 10 m margin
                         "0084d717" "80e84bd3" // 40.0000000, -75.0000000
                         "0084d717" "a0364cd3" // 40.0000000, -74.9980000
                         "20d2d717" "a0364cd3" // 40.0020000, -74.9980000
                         "20d2d717" "80e84bd3");

    // Rejections.
    CHECK(!decodeFenceSet(buf, n - 1, 1, g)); // truncated
    buf[0] = 9;
    CHECK(!decodeFenceSet(buf, n, 1, g)); // too many zones
    const uint8_t junk[] = {2, 4, 0, 0};
    CHECK(!messageType(junk, sizeof(junk), type)); // wrong protocol version
}

int main()
{
    testCaiXianLin();
    testGeometry();
    testStateMachine();
    testShockCaps();
    testProtocol();
    std::printf("%d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
