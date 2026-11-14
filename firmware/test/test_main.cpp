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
static Zone square(ZoneKind kind = ZoneKind::KeepIn, uint8_t danger = 0, uint8_t warning = 5, uint8_t attention = 5)
{
    Zone z;
    z.kind = kind;
    z.dangerM = danger;
    z.warningM = warning;
    z.attentionM = attention;
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
    f.zones[0] = square(ZoneKind::KeepIn, 0, 5, 5); // no danger band: 5 m vibrate, 5 m beep
    e.setFences(f);
    CHECK(e.state() == CollarState::Inside);

    uint32_t t = 1000;
    // Well inside.
    Decision d = e.onFix(fixAt(100, 100, t += 1000));
    CHECK(d.correction == Correction::None && e.state() == CollarState::Inside);

    // 8 m from the line: attention band, beep.
    d = e.onFix(fixAt(8, 100, t += 1000));
    CHECK(d.correction == Correction::Beep && e.state() == CollarState::Warning && d.sendStatus);

    // 3 m from the line: warning band, vibrate.
    d = e.onFix(fixAt(3, 100, t += 1000));
    CHECK(d.correction == Correction::Vibrate);

    // A poor fix outside is ignored entirely.
    d = e.onFix(fixAt(-20, 100, t += 1000, 900, 9));
    CHECK(d.fixRejected && d.correction == Correction::None && e.state() == CollarState::Warning);
    d = e.onFix(fixAt(-20, 100, t += 1000, 150, 3));
    CHECK(d.fixRejected);

    // Past the line: corrections stop straight away.
    d = e.onFix(fixAt(-5, 100, t += 1000));
    CHECK(e.state() == CollarState::Outside && d.correction == Correction::None && d.sendStatus);
    d = e.onFix(fixAt(-8, 100, t += 1000));
    CHECK(d.correction == Correction::None);
    // Third consecutive outside fix: lost. Breach sent, corrections stop.
    d = e.onFix(fixAt(-12, 100, t += 1000));
    CHECK(e.state() == CollarState::Lost && d.sendBreach && d.correction == Correction::None);
    d = e.onFix(fixAt(-40, 100, t += 1000));
    CHECK(d.correction == Correction::None && !d.sendBreach);

    // Coming back: needs rearmFixes good fixes clear of every band (10 m here).
    d = e.onFix(fixAt(8, 100, t += 1000)); // inside, but still in the attention band
    CHECK(e.state() == CollarState::Lost && d.correction == Correction::None);
    e.onFix(fixAt(20, 100, t += 1000));
    e.onFix(fixAt(20, 100, t += 1000));
    d = e.onFix(fixAt(20, 100, t += 1000));
    CHECK(e.state() == CollarState::Inside && d.sendStatus);

    // A short trip past the line (not long enough to be lost): silent on the way back in through
    // the bands, and only corrected again once clear of them all.
    d = e.onFix(fixAt(-2, 100, t += 1000));
    CHECK(e.state() == CollarState::Outside && d.correction == Correction::None);
    d = e.onFix(fixAt(3, 100, t += 1000)); // warning band, coming back
    CHECK(e.state() == CollarState::Warning && d.correction == Correction::None);
    d = e.onFix(fixAt(8, 100, t += 1000)); // attention band
    CHECK(d.correction == Correction::None);
    d = e.onFix(fixAt(30, 100, t += 1000)); // clear: re-armed
    CHECK(e.state() == CollarState::Inside);
    d = e.onFix(fixAt(8, 100, t += 1000));
    CHECK(d.correction == Correction::Beep);
}

static void testGpsPacing()
{
    Settings s;
    CHECK(gpsIntervalSecs(s, CollarState::NoFence, 500, 10) == 0);
    CHECK(gpsIntervalSecs(s, CollarState::Inside, 1000, 10) == 60); // deep inside: slow
    CHECK(gpsIntervalSecs(s, CollarState::Inside, 10 + 6 * 30, 10) == 30); // 180 m of room at 6 m/s
    CHECK(gpsIntervalSecs(s, CollarState::Inside, 20, 10) == 5);   // close: fast
    CHECK(gpsIntervalSecs(s, CollarState::Inside, 5, 10) == 5);    // inside the margin
    CHECK(gpsIntervalSecs(s, CollarState::Warning, 500, 10) == 5);
    CHECK(gpsIntervalSecs(s, CollarState::Outside, -5, 10) == 5);
    CHECK(gpsIntervalSecs(s, CollarState::Lost, -500, 10) == 15);
    s.gpsPacing = false;
    CHECK(gpsIntervalSecs(s, CollarState::Inside, 20, 10) == 0);
    s.gpsPacing = true;
    s.dogSpeedMps = 0;
    CHECK(gpsIntervalSecs(s, CollarState::Inside, 1000, 10) == 5); // nonsense settings: stay safe
}

static void testShockCaps()
{
    FenceEngine e;
    FenceSet f;
    f.zoneCount = 1;
    f.zones[0] = square(ZoneKind::KeepIn, 3, 5, 5); // with a 3 m danger band
    e.setFences(f);
    Settings &s = e.settings();
    s.shockEnabled = true;
    s.shockLevel = 60;
    s.maxShockLevel = 30;
    s.lostConfirmFixes = 10; // keep it in Outside for this test
    s.maxShocksPerBreach = 2;
    s.maxShocksPerHour = 3;

    // 1.5 m inside the line: in the danger band.
    uint32_t t = 0;
    Decision d = e.onFix(fixAt(1.5, 100, t += 1000));
    CHECK(d.correction == Correction::Shock && d.intensity == 30); // capped level
    d = e.onFix(fixAt(1.5, 100, t += 1000));
    CHECK(d.correction == Correction::Shock);
    d = e.onFix(fixAt(1.5, 100, t += 1000));
    CHECK(d.correction == Correction::Vibrate); // per-breach cap reached

    // Back clear resets the per-breach count, but the hourly cap (3) still applies.
    e.onFix(fixAt(100, 100, t += 1000));
    d = e.onFix(fixAt(1.5, 100, t += 1000));
    CHECK(d.correction == Correction::Shock);
    e.onFix(fixAt(100, 100, t += 1000));
    d = e.onFix(fixAt(1.5, 100, t += 1000));
    CHECK(d.correction == Correction::Vibrate); // 3 shocks in the last hour

    // An hour later shocks are allowed again.
    e.onFix(fixAt(100, 100, t += 3600000));
    d = e.onFix(fixAt(1.5, 100, t += 1000));
    CHECK(d.correction == Correction::Shock);

    // Never a shock past the line.
    d = e.onFix(fixAt(-1, 100, t += 1000));
    CHECK(d.correction == Correction::None);

    // Corrections off: nothing at all.
    s.correctionsEnabled = false;
    e.onFix(fixAt(100, 100, t += 1000));
    d = e.onFix(fixAt(1.5, 100, t += 1000));
    CHECK(d.correction == Correction::None);
}

static void testBands()
{
    FenceEngine e;
    e.settings().shockEnabled = true;
    e.settings().lostConfirmFixes = 10;
    FenceSet f;
    f.zoneCount = 1;
    f.zones[0] = square(ZoneKind::KeepIn, 3, 4, 6); // shock 0-3 m, vibrate 3-7 m, beep 7-13 m
    e.setFences(f);
    uint32_t t = 0;
    CHECK(e.onFix(fixAt(20, 100, t += 1000)).correction == Correction::None);
    CHECK(e.onFix(fixAt(10, 100, t += 1000)).correction == Correction::Beep);
    CHECK(e.onFix(fixAt(5, 100, t += 1000)).correction == Correction::Vibrate);
    Decision d = e.onFix(fixAt(1.5, 100, t += 1000));
    CHECK(d.correction == Correction::Shock && e.state() == CollarState::Warning); // danger band, still inside
    CHECK(bandFor(f.zones[0], 12.9f) == Band::Attention && bandFor(f.zones[0], 13.1f) == Band::Clear);
    CHECK(bandFor(f.zones[0], -0.1f) == Band::Outside && bandFor(f.zones[0], 2.9f) == Band::Danger);
    CHECK(alertWidthM(f) == 13);

    // Danger band off: the zone never shocks, even outside and with shock enabled on the collar.
    FenceEngine soft;
    soft.settings().shockEnabled = true;
    soft.settings().lostConfirmFixes = 10;
    f.zones[0] = square(ZoneKind::KeepIn, 0, 4, 6);
    soft.setFences(f);
    CHECK(soft.onFix(fixAt(1, 100, t += 1000)).correction == Correction::Vibrate);
    CHECK(soft.onFix(fixAt(-3, 100, t += 1000)).correction == Correction::None);

    // Shock disabled on the collar: a danger band vibrates instead.
    FenceEngine off;
    f.zones[0] = square(ZoneKind::KeepIn, 3, 4, 6);
    off.setFences(f);
    CHECK(off.onFix(fixAt(1, 100, t += 1000)).correction == Correction::Vibrate);

    // Several zones: the most severe band wins. A keep-out pond's bands lie outside the pond.
    FenceEngine two;
    FenceSet g;
    g.zoneCount = 2;
    g.zones[0] = square(ZoneKind::KeepIn, 0, 5, 5);
    g.zones[1] = square(ZoneKind::KeepOut, 0, 5, 5);
    for (int i = 0; i < 4; i++) { // shrink the pond to the middle: 80..140 m north, 60..110 m east
        g.zones[1].latI[i] = latAt(i < 2 ? 80 : 140);
        g.zones[1].lonI[i] = lonAt(i == 0 || i == 3 ? 60 : 110);
    }
    two.setFences(g);
    CHECK(two.onFix(fixAt(110, 30, t += 1000)).correction == Correction::None); // 30 m from both
    CHECK(two.onFix(fixAt(110, 52, t += 1000)).correction == Correction::Beep);  // 8 m from the pond
    CHECK(two.onFix(fixAt(110, 57, t += 1000)).correction == Correction::Vibrate);
}

static void testPulser()
{
    FenceSet f;
    f.beepPulseS = 3;
    f.vibratePulseS = 2;
    CorrectionPulser p;
    uint8_t level = 0;
    Decision beep;
    beep.correction = Correction::Beep;
    Decision buzz;
    buzz.correction = Correction::Vibrate;
    buzz.intensity = 50;

    CHECK(p.onDecision(beep, f, 10000));                                       // new: fire now
    CHECK(p.poll(f, 11000, true, level) == Correction::None);                  // too soon
    CHECK(p.poll(f, 13000, true, level) == Correction::Beep);                  // 3 s later
    CHECK(!p.onDecision(beep, f, 14000));                                      // fix 1 s after a pulse: wait
    CHECK(p.poll(f, 16000, true, level) == Correction::Beep);
    CHECK(p.onDecision(buzz, f, 16500));                                       // escalation fires at once
    CHECK(p.poll(f, 18500, true, level) == Correction::Vibrate && level == 50); // every 2 s
    CHECK(p.poll(f, 19000, false, level) == Correction::None);                 // corrections off: stops
    CHECK(p.poll(f, 21000, true, level) == Correction::None);                  // and stays stopped
    CHECK(p.onDecision(buzz, f, 22000));
    CHECK(p.poll(f, 22000 + CorrectionPulser::kStaleMs + 1, true, level) == Correction::None); // no fresh fix
    Decision none;
    p.onDecision(buzz, f, 60000);
    CHECK(!p.onDecision(none, f, 61000));                                      // clear: nothing,
    CHECK(p.poll(f, 70000, true, level) == Correction::None);                  // and pulsing ends

    FenceSet once; // pulse 0: once per fix, no repeats
    CorrectionPulser q;
    CHECK(q.onDecision(beep, once, 1000) && q.onDecision(beep, once, 1500));
    CHECK(q.poll(once, 9000, true, level) == Correction::None);
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
    CHECK(hex(buf, n) == "02040228dc05");
    Command c2{};
    CHECK(decodeCommand(buf, n, c2) && c2.action == CommandAction::Vibrate && c2.intensity == 40 && c2.durationMs == 1500);

    StatusReport s{3, 4, 87, 400001234, -750004321, 180, 9, -12, 1};
    n = encodeStatus(buf, sizeof(buf), MsgType::Breach, s);
    CHECK(n == kStatusLength);
    CHECK(hex(buf, n) == "020303000457d288d7179fd74bd3b40009f4ff01");
    StatusReport s2{};
    CHECK(decodeStatus(buf, n, s2) && s2.latI == 400001234 && s2.lonI == -750004321 && s2.clearanceM == -12 &&
          s2.state == 4);
    MsgType type;
    CHECK(messageType(buf, n, type) && type == MsgType::Breach);

    // Fence set round trip, chunked and reassembled out of order.
    FenceSet f;
    f.zoneCount = 2;
    f.zones[0] = square(ZoneKind::KeepIn, 2, 4, 6);
    f.zones[1] = square(ZoneKind::KeepOut, 0, 5, 5);
    f.zones[1].vertexCount = 32; // make it big enough to need several chunks
    for (int i = 4; i < 32; i++) {
        f.zones[1].latI[i] = 400000000 + i * 100;
        f.zones[1].lonI[i] = -750000000 + i * 100;
    }
    uint8_t blob[kMaxFenceBlob];
    const size_t blobLen = encodeFenceSet(blob, sizeof(blob), f);
    CHECK(blobLen == 3 + (6 + 4 * 8) + (6 + 32 * 8));

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
    CHECK(g.version == 9 && g.zoneCount == 2 && g.zones[0].dangerM == 2 && g.zones[0].warningM == 4 && g.zones[0].attentionM == 6 && g.zones[1].kind == ZoneKind::KeepOut &&
          g.zones[1].vertexCount == 32 && g.zones[1].lonI[31] == -750000000 + 3100);

    // Golden fence blob for one small zone, shared with the Python tests.
    FenceSet one;
    one.zoneCount = 1;
    one.zones[0] = square(ZoneKind::KeepIn, 3, 5, 7);
    one.beepPulseS = 4;
    one.vibratePulseS = 2;
    n = encodeFenceSet(buf, sizeof(buf), one);
    CHECK(hex(buf, n) == "01" "04" "02"                 // 1 zone, beep every 4 s, vibrate every 2 s
                         "00" "04" "03" "05" "07" "00" // keep-in, 4 vertices, bands 3/5/7 m
                         "0084d717" "80e84bd3" // 40.0000000, -75.0000000
                         "0084d717" "a0364cd3" // 40.0000000, -74.9980000
                         "20d2d717" "a0364cd3" // 40.0020000, -74.9980000
                         "20d2d717" "80e84bd3");

    // Rejections.
    CHECK(!decodeFenceSet(buf, n - 1, 1, g)); // truncated
    buf[0] = 9;
    CHECK(!decodeFenceSet(buf, n, 1, g)); // too many zones
    const uint8_t junk[] = {1, 4, 0, 0}; // version 1 had a different fence format
    CHECK(!messageType(junk, sizeof(junk), type)); // wrong protocol version
}

int main()
{
    testCaiXianLin();
    testGeometry();
    testStateMachine();
    testShockCaps();
    testBands();
    testPulser();
    testGpsPacing();
    testProtocol();
    std::printf("%d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
