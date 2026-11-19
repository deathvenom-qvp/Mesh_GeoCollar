// SPDX-License-Identifier: GPL-3.0-or-later
//
// Host tests for the parts of GeoCollar that do not need hardware: the CaiXianLin frame builder,
// the fence engine, the wire protocol and the companion app's link. Build and run with
// `make -C firmware/test`.

#include "../GeoCollar/AppLink.h"
#include "../GeoCollar/CaiXianLin.h"
#include "../GeoCollar/Fence.h"
#include "../GeoCollar/Protocol.h"
#include "../GeoCollar/Sha256.h"

#include <cmath>
#include <cstdio>
#include <cstring>
#include <new>
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

static Fix fixAt(double northM, double eastM, uint32_t t, uint16_t pdop = 150, uint8_t sats = 14)
{
    return Fix{latAt(northM), lonAt(eastM), pdop, sats, t};
}

/// Two readings at the same place, a second apart: the first only starts the wait, the second is
/// the second look. Returns what the second one decides.
static Decision twice(FenceEngine &e, double northM, double eastM, uint32_t &t)
{
    e.onFix(fixAt(northM, eastM, t += 1000));
    return e.onFix(fixAt(northM, eastM, t += 1000));
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

    // 8 m from the line: attention band. The first reading only starts the wait; the second,
    // a second later, beeps.
    d = e.onFix(fixAt(8, 100, t += 1000));
    CHECK(d.correction == Correction::None && d.verify && e.state() == CollarState::Warning && d.sendStatus);
    d = e.onFix(fixAt(8, 100, t += 1000));
    CHECK(d.correction == Correction::Beep && !d.verify && !d.sendStatus);

    // 3 m from the line: warning band. Still a beep until the second look, then vibrate.
    d = e.onFix(fixAt(3, 100, t += 1000));
    CHECK(d.correction == Correction::Beep && d.verify);
    d = e.onFix(fixAt(3, 100, t += 1000));
    CHECK(d.correction == Correction::Vibrate);

    // A poor fix outside is ignored entirely.
    d = e.onFix(fixAt(-20, 100, t += 1000, 900, 9));
    CHECK(d.fixRejected && d.correction == Correction::None && e.state() == CollarState::Warning);
    d = e.onFix(fixAt(-20, 100, t += 1000, 150, 3));
    CHECK(d.fixRejected);
    // 12 satellites or more, or the fix does not count: 11 is ignored, as is a count the GPS did not
    // give. 12 is enough.
    CHECK(kMinFixSats == 12 && e.settings().minSats == 12);
    d = e.onFix(fixAt(-20, 100, t += 1000, 150, 11));
    CHECK(d.fixRejected && e.state() == CollarState::Warning);
    d = e.onFix(fixAt(-20, 100, t += 1000, 150, 0));
    CHECK(d.fixRejected && !e.fixIsGood(fixAt(-20, 100, t, 150, 0)));
    CHECK(e.fixIsGood(fixAt(-20, 100, t, 150, 12)) && !e.fixIsGood(fixAt(-20, 100, t, 150, 11)));
    // Home mode alone takes a fix from 8 satellites, only kHomeMarginM or more clear of every band
    // (10 m of bands here), with a usable PDOP.
    CHECK(kHomeMinSats == 8 && kHomeMarginM == 10.0f);
    CHECK(e.clearForHome(fixAt(30, 100, t, 150, 8)) && !e.clearForHome(fixAt(30, 100, t, 150, 7)));
    CHECK(!e.clearForHome(fixAt(30, 100, t, 900, 10)) && !e.clearForHome(fixAt(18, 100, t, 150, 10)));
    CHECK(!e.clearForHome(fixAt(-20, 100, t, 150, 10)) && !e.clearForHome(Fix{0, 0, 150, 10, t}));
    CHECK(!FenceEngine().clearForHome(fixAt(30, 100, t, 150, 10))); // no fences

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
    d = twice(e, 8, 100, t);
    CHECK(d.correction == Correction::Beep);
}

/// New fences while the dog is out: it stays out (or lost), and is not corrected on its way back.
static void testFenceReload()
{
    FenceSet f;
    f.version = 1;
    f.zoneCount = 1;
    f.zones[0] = square(ZoneKind::KeepIn, 3, 5, 5);
    FenceSet g = f;
    g.version = 2;
    g.zones[0].attentionM = 6; // the owner widens a band while the dog is out
    uint32_t t = 1000;

    // Past the line, not yet lost: new fences keep it Outside, and quiet through the bands.
    FenceEngine e;
    e.setFences(f);
    e.onFix(fixAt(-5, 100, t += 1000));
    CHECK(e.state() == CollarState::Outside);
    e.setFences(g);
    CHECK(e.state() == CollarState::Outside && e.fences().version == 2);
    Decision d = twice(e, 2, 100, t); // danger band, coming back
    CHECK(e.state() == CollarState::Warning && d.correction == Correction::None);
    d = twice(e, 6, 100, t); // warning band
    CHECK(d.correction == Correction::None);
    d = e.onFix(fixAt(40, 100, t += 1000)); // clear: re-armed
    CHECK(e.state() == CollarState::Inside);
    d = twice(e, 6, 100, t);
    CHECK(d.correction == Correction::Vibrate);

    // Still outside: the fix after the reload counts on towards lost.
    FenceEngine o;
    o.setFences(f);
    o.onFix(fixAt(-5, 100, t += 1000));
    o.onFix(fixAt(-6, 100, t += 1000));
    o.setFences(g);
    d = o.onFix(fixAt(-7, 100, t += 1000));
    CHECK(o.state() == CollarState::Lost && d.sendBreach);

    // Lost: new fences leave it lost; only rearmFixes clear fixes bring it back.
    FenceEngine l;
    l.setFences(f);
    for (int i = 0; i < 3; i++)
        l.onFix(fixAt(-20, 100, t += 1000));
    CHECK(l.state() == CollarState::Lost);
    l.setFences(g);
    CHECK(l.state() == CollarState::Lost);
    d = twice(l, 2, 100, t);
    CHECK(l.state() == CollarState::Lost && d.correction == Correction::None);
    for (int i = 0; i < 3; i++)
        d = l.onFix(fixAt(40, 100, t += 1000));
    CHECK(l.state() == CollarState::Inside && d.sendStatus);

    // Inside, or no fences at all: a fresh start, as before.
    FenceEngine in;
    in.setFences(f);
    in.onFix(fixAt(40, 100, t += 1000));
    in.setFences(g);
    CHECK(in.state() == CollarState::Inside);
    FenceSet none;
    l.forceLost();
    l.setFences(none);
    CHECK(l.state() == CollarState::NoFence);
}

static void testGpsPacing()
{
    Settings s;
    CHECK(gpsIntervalSecs(s, CollarState::NoFence, 500, 10) == 0);
    CHECK(gpsIntervalSecs(s, CollarState::Inside, 1000, 10) == kFarGpsSecs); // deep inside: slow
    CHECK(gpsIntervalSecs(s, CollarState::Inside, 10 + 100, 10) == 120);     // 100 m in
    CHECK(gpsIntervalSecs(s, CollarState::Inside, 20, 10) == 5);   // close: fast
    CHECK(gpsIntervalSecs(s, CollarState::Inside, 5, 10) == 5);    // inside the margin
    CHECK(gpsIntervalSecs(s, CollarState::Warning, 500, 10) == 5);
    CHECK(gpsIntervalSecs(s, CollarState::Outside, -5, 10) == 5);
    CHECK(gpsIntervalSecs(s, CollarState::Lost, -500, 10) == kLostGpsSecs);
    s.gpsPacing = false;
    CHECK(gpsIntervalSecs(s, CollarState::Inside, 20, 10) == 0);
    s.gpsPacing = true;

    // What Meshtastic is given so that, with its hold after each lock, fixes come as wanted.
    CHECK(gpsSettingSecs(0) == 0);
    CHECK(gpsSettingSecs(5) == 5);   // GPS always on: unchanged
    CHECK(gpsSettingSecs(10) == 10);
    CHECK(gpsSettingSecs(11) == 10); // 10.5 rounds down to always on
    CHECK(gpsSettingSecs(12) == 11); // 11 s + a 1 s hold
    CHECK(gpsSettingSecs(15) == 12); // lost: 12 s + 2 s, a fix every 14 s
    CHECK(gpsSettingSecs(30) == 20); // beacon: 20 s + 10 s
    CHECK(gpsSettingSecs(50) == 30); // 30 s + the full 20 s
    CHECK(gpsSettingSecs(60) == 40); // deep inside: 40 s + 20 s
    bool late = false, early = false;
    for (uint32_t want = 1; want <= 3600; want++) {
        const uint32_t set = gpsSettingSecs(want);
        const uint32_t hold = set <= 10 ? 0 : (set - 10 < 20 ? set - 10 : 20);
        late = late || set + hold > want;                    // never later than wanted
        early = early || (want > 10 && set + hold + 1 < want); // and at most a second early
    }
    CHECK(!late);
    CHECK(!early);
}

static void testFixWatch()
{
    CHECK(fixOverdueMs(0) == 180 * 1000u);  // Meshtastic's default, 2 minutes, plus one
    CHECK(fixOverdueMs(5) == 45 * 1000u);   // the GPS stays on
    CHECK(fixOverdueMs(10) == 45 * 1000u);
    CHECK(fixOverdueMs(11) == 71 * 1000u);
    CHECK(fixOverdueMs(60) == 120 * 1000u);
    CHECK(fixOverdueMs(100000) == 3660 * 1000u);

    // No fix at boot, whatever the time.
    FixWatch w;
    CHECK(w.noFix());
    CHECK(w.tick(1000, 5));
    CHECK(w.tick(500000, 5));

    // A fix counts at once, before the next tick, so a report sent with it says so.
    w.onFix(500000);
    CHECK(!w.noFix());
    CHECK(!w.tick(500000 + 45000, 5));
    CHECK(w.tick(500000 + 45001, 5)); // overdue
    CHECK(w.tick(500000 + 46000, 60)); // a slower interval does not bring an old fix back
    CHECK(w.noFix());
    w.onFix(600000);
    CHECK(!w.tick(600001, 60));

    // Slow to fast: the last fix may be older than the fast allowance already. The GPS gets a
    // full allowance from the change before the fix counts as old.
    FixWatch v;
    v.tick(0, 60);
    v.onFix(1000);
    CHECK(!v.tick(101000, 60));          // 100 s old, 120 s allowed
    CHECK(!v.tick(102000, 5));           // the interval drops to 5 s: not overdue yet
    CHECK(!v.tick(102000 + 45000, 5));
    CHECK(v.tick(102000 + 45001, 5));    // 45 s after the change and still nothing
    v.onFix(150000);
    CHECK(!v.tick(150000 + 45000, 5));
    CHECK(v.tick(150000 + 45001, 5));    // later on, the usual allowance from the fix itself

    // Fast to slow gives more time, never less.
    FixWatch u;
    u.tick(0, 5);
    u.onFix(1000);
    CHECK(!u.tick(40000, 5));
    CHECK(!u.tick(41000, 60));
    CHECK(!u.tick(121000, 60));
    CHECK(!u.tick(41000 + 120000, 60));
    CHECK(u.tick(41000 + 120001, 60));

    // The millisecond clock wraps after 49.7 days: ages are still right across the wrap, and an
    // interval set long ago does not hand out a second allowance.
    FixWatch x;
    x.tick(0xffff0000u, 5);
    x.onFix(0xffffff00u);
    CHECK(!x.tick(0x00000100u, 5));              // 512 ms later, across the wrap
    CHECK(!x.tick(0xffffff00u + 45000u, 5));
    CHECK(x.tick(0xffffff00u + 45001u, 5));
    FixWatch y;
    y.tick(1000, 5);
    y.onFix(2000);
    CHECK(!y.tick(47000, 5));                    // also ends the allowance from the first interval
    y.onFix(0xfffe0000u);
    CHECK(!y.tick(0xfffe0000u + 45000u, 5));
    CHECK(y.tick(1000u + 10u, 5));               // the clock is back where the interval was set: still overdue
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

    // 1.5 m inside the line: in the danger band. One reading there never shocks.
    uint32_t t = 0;
    Decision d = e.onFix(fixAt(1.5, 100, t += 1000));
    CHECK(d.correction == Correction::None && d.verify);
    d = e.onFix(fixAt(1.5, 100, t += 1000));
    CHECK(d.correction == Correction::Shock && d.intensity == 30); // capped level
    d = e.onFix(fixAt(1.5, 100, t += 1000));
    CHECK(d.correction == Correction::Shock);
    d = e.onFix(fixAt(1.5, 100, t += 1000));
    CHECK(d.correction == Correction::Vibrate); // per-breach cap reached

    // Back clear resets the per-breach count, but the hourly cap (3) still applies.
    e.onFix(fixAt(100, 100, t += 1000));
    d = twice(e, 1.5, 100, t);
    CHECK(d.correction == Correction::Shock);
    e.onFix(fixAt(100, 100, t += 1000));
    d = twice(e, 1.5, 100, t);
    CHECK(d.correction == Correction::Vibrate); // 3 shocks in the last hour

    // An hour later shocks are allowed again.
    e.onFix(fixAt(100, 100, t += 3600000));
    d = twice(e, 1.5, 100, t);
    CHECK(d.correction == Correction::Shock);

    // Never a shock past the line.
    d = e.onFix(fixAt(-1, 100, t += 1000));
    CHECK(d.correction == Correction::None);

    // Corrections off: nothing at all.
    s.correctionsEnabled = false;
    e.onFix(fixAt(100, 100, t += 1000));
    d = twice(e, 1.5, 100, t);
    CHECK(d.correction == Correction::None && !d.verify);
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
    CHECK(twice(e, 10, 100, t).correction == Correction::Beep);
    CHECK(twice(e, 5, 100, t).correction == Correction::Vibrate);
    Decision d = twice(e, 1.5, 100, t);
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
    CHECK(twice(soft, 1, 100, t).correction == Correction::Vibrate);
    CHECK(soft.onFix(fixAt(-3, 100, t += 1000)).correction == Correction::None);

    // Shock disabled on the collar: a danger band vibrates instead.
    FenceEngine off;
    f.zones[0] = square(ZoneKind::KeepIn, 3, 4, 6);
    off.setFences(f);
    CHECK(twice(off, 1, 100, t).correction == Correction::Vibrate);

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
    CHECK(twice(two, 110, 52, t).correction == Correction::Beep); // 8 m from the pond
    CHECK(twice(two, 110, 57, t).correction == Correction::Vibrate);
}

// The second look: no correction on the strength of one GPS reading.
static void testSecondLook()
{
    FenceSet f;
    f.zoneCount = 1;
    f.zones[0] = square(ZoneKind::KeepIn, 3, 4, 6); // shock 0-3 m, vibrate 3-7 m, beep 7-13 m
    const double kBeep = 10, kBuzz = 5, kShock = 1.5, kClear = 50;
    auto fresh = [&f]() {
        FenceEngine e;
        e.settings().shockEnabled = true;
        e.settings().lostConfirmFixes = 10;
        e.setFences(f);
        return e;
    };

    // The first reading in a band starts the wait; a reading before the second is over does not
    // end it; the first one after it does.
    FenceEngine e = fresh();
    uint32_t t = 5000;
    Decision d = e.onFix(fixAt(kBeep, 100, t));
    CHECK(d.correction == Correction::None && d.verify && d.verifyAtMs == t + kVerifyMs);
    CHECK(e.state() == CollarState::Warning); // the state says where the dog is, the wait or not
    d = e.onFix(fixAt(kBeep, 100, t + 600));
    CHECK(d.correction == Correction::None && d.verify && d.verifyAtMs == t + kVerifyMs);
    d = e.onFix(fixAt(kBeep, 100, t + kVerifyMs));
    CHECK(d.correction == Correction::Beep && !d.verify);
    // After that every reading in the band corrects at once, however close together.
    d = e.onFix(fixAt(kBeep, 100, t + kVerifyMs + 100));
    CHECK(d.correction == Correction::Beep && !d.verify);

    // The second look finds the dog clear of the bands: no correction, and the next time in a
    // band waits again.
    e = fresh();
    t = 5000;
    CHECK(e.onFix(fixAt(kShock, 100, t)).verify);
    d = e.onFix(fixAt(kClear, 100, t += 1000));
    CHECK(d.correction == Correction::None && !d.verify && e.state() == CollarState::Inside);
    d = e.onFix(fixAt(kShock, 100, t += 1000));
    CHECK(d.correction == Correction::None && d.verify);
    // Also when a reading before the second was over had it clear.
    d = e.onFix(fixAt(kClear, 100, t += 300));
    CHECK(!d.verify);
    d = e.onFix(fixAt(kShock, 100, t += 700));
    CHECK(d.correction == Correction::None && d.verify && d.verifyAtMs == t + kVerifyMs);

    // The second look finds the dog past the line: nothing, as always out there.
    e = fresh();
    t = 5000;
    e.onFix(fixAt(kBuzz, 100, t));
    d = e.onFix(fixAt(-2, 100, t += 1000));
    CHECK(d.correction == Correction::None && !d.verify && e.state() == CollarState::Outside);
    d = e.onFix(fixAt(kBuzz, 100, t += 1000)); // on the way back in: silent, and no wait running
    CHECK(d.correction == Correction::None && !d.verify);

    // A step up to a stronger correction takes a second look too. Meanwhile the correction that
    // is already agreed on goes on.
    e = fresh();
    t = 5000;
    CHECK(twice(e, kBeep, 100, t).correction == Correction::Beep);
    d = e.onFix(fixAt(kBuzz, 100, t += 1000));
    CHECK(d.correction == Correction::Beep && d.verify && d.verifyAtMs == t + kVerifyMs);
    d = e.onFix(fixAt(kBuzz, 100, t += 1000));
    CHECK(d.correction == Correction::Vibrate && !d.verify);
    d = e.onFix(fixAt(kShock, 100, t += 1000));
    CHECK(d.correction == Correction::Vibrate && d.verify);
    d = e.onFix(fixAt(kShock, 100, t += 1000));
    CHECK(d.correction == Correction::Shock && !d.verify);
    // A step down counts at once, and then going deeper again is checked again.
    d = e.onFix(fixAt(kBeep, 100, t += 1000));
    CHECK(d.correction == Correction::Beep && !d.verify);
    d = e.onFix(fixAt(kShock, 100, t += 1000));
    CHECK(d.correction == Correction::Beep && d.verify);

    // Two readings in different bands agree on the milder one. Straight into the danger band,
    // then the warning band: vibrate, and nothing left to wait for.
    e = fresh();
    t = 5000;
    e.onFix(fixAt(kShock, 100, t));
    d = e.onFix(fixAt(kBuzz, 100, t += 1000));
    CHECK(d.correction == Correction::Vibrate && !d.verify);
    // The other way round: vibrate, and the danger band still needs its own second look.
    e = fresh();
    t = 5000;
    e.onFix(fixAt(kBuzz, 100, t));
    d = e.onFix(fixAt(kShock, 100, t += 1000));
    CHECK(d.correction == Correction::Vibrate && d.verify && d.verifyAtMs == t + kVerifyMs);
    d = e.onFix(fixAt(kShock, 100, t += 1000));
    CHECK(d.correction == Correction::Shock);
    // A milder reading during the wait counts as well: danger, warning, danger within the second
    // is only agreed on the warning band.
    e = fresh();
    t = 5000;
    e.onFix(fixAt(kShock, 100, t));
    e.onFix(fixAt(kBuzz, 100, t + 400));
    d = e.onFix(fixAt(kShock, 100, t + 1000));
    CHECK(d.correction == Correction::Vibrate && d.verify);

    // A reading too poor to use is no second look, and the wait stays open.
    e = fresh();
    t = 5000;
    e.onFix(fixAt(kBuzz, 100, t));
    d = e.onFix(fixAt(kBuzz, 100, t += 1000, 900, 9));
    CHECK(d.fixRejected && d.correction == Correction::None && d.verify && d.verifyAtMs == 5000 + kVerifyMs);
    d = e.onFix(fixAt(kBuzz, 100, t += 1000));
    CHECK(d.correction == Correction::Vibrate);

    // A first reading that has got old is not agreed with: the wait starts again.
    e = fresh();
    t = 5000;
    e.onFix(fixAt(kBuzz, 100, t));
    d = e.onFix(fixAt(kBuzz, 100, t += kVerifyStaleMs + 1));
    CHECK(d.correction == Correction::None && d.verify && d.verifyAtMs == t + kVerifyMs);
    d = e.onFix(fixAt(kBuzz, 100, t += kVerifyStaleMs)); // just young enough
    CHECK(d.correction == Correction::Vibrate);

    // With corrections off there is nothing to wait for, but the readings still count, so
    // turning corrections back on does not start from a band agreed on long ago.
    e = fresh();
    t = 5000;
    twice(e, kShock, 100, t); // shocked once
    e.settings().correctionsEnabled = false;
    d = e.onFix(fixAt(kBeep, 100, t += 1000));
    CHECK(d.correction == Correction::None && !d.verify);
    e.settings().correctionsEnabled = true;
    d = e.onFix(fixAt(kShock, 100, t += 1000));
    CHECK(d.correction == Correction::Beep && d.verify);

    // New fences, and the owner's lost and found commands, start afresh.
    e = fresh();
    t = 5000;
    twice(e, kBuzz, 100, t);
    e.setFences(f);
    CHECK(e.onFix(fixAt(kBuzz, 100, t += 1000)).correction == Correction::None);
    twice(e, kBuzz, 100, t);
    e.forceLost();
    e.clearLost();
    d = e.onFix(fixAt(kBuzz, 100, t += 1000));
    CHECK(d.correction == Correction::None && d.verify);

    // The millisecond clock wrapping round during the wait changes nothing.
    e = fresh();
    t = UINT32_MAX - 400;
    d = e.onFix(fixAt(kBuzz, 100, t));
    CHECK(d.verify && d.verifyAtMs == 599);
    CHECK(e.onFix(fixAt(kBuzz, 100, 100)).correction == Correction::None); // 501 ms later
    CHECK(e.onFix(fixAt(kBuzz, 100, 599)).correction == Correction::Vibrate);
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
    Command c{CommandAction::Vibrate, 40, 1500, 1763085600};
    size_t n = encodeCommand(buf, sizeof(buf), c);
    CHECK(n == kCommandLength && hex(buf, n) == "02040228dc05208d1669");
    Command c2{};
    CHECK(decodeCommand(buf, n, c2) && c2.action == CommandAction::Vibrate && c2.intensity == 40 && c2.durationMs == 1500 &&
          c2.seq == 1763085600);
    CHECK(!decodeCommand(buf, 6, c2)); // the old form, without a sequence number, is refused
    CHECK(fenceVersionAcceptable(5, 4) && fenceVersionAcceptable(4, 4) && !fenceVersionAcceptable(3, 4));
    CHECK(fenceVersionAcceptable(2, 65530) && !fenceVersionAcceptable(65530, 2) && fenceVersionAcceptable(1, 0));

    // Owner and Config carry an optional sequence number; golden vectors shared with the Python tests.
    OwnerInfo who;
    std::strcpy(who.dog, "Rex");
    std::strcpy(who.owner, "Ken");
    std::strcpy(who.phone, "5551234567");
    uint32_t seq = 99;
    OwnerInfo who2;
    n = encodeOwner(buf, sizeof(buf), who);
    CHECK(hex(buf, n) == "0205" "03526578" "034b656e" "0a35353531323334353637");
    CHECK(decodeOwner(buf, n, who2, seq) && seq == 0 && std::strcmp(who2.dog, "Rex") == 0);
    n = encodeOwner(buf, sizeof(buf), who, 1763085600);
    CHECK(hex(buf, n) == "0205" "03526578" "034b656e" "0a35353531323334353637" "208d1669");
    CHECK(decodeOwner(buf, n, who2, seq) && seq == 1763085600 && std::strcmp(who2.phone, "5551234567") == 0);
    CHECK(!decodeOwner(buf, n - 1, who2, seq) && !decodeOwner(buf, n - 3, who2, seq)); // a cut-off number
    buf[n] = 0;
    CHECK(!decodeOwner(buf, n + 1, who2, seq)); // anything after the number
    std::memset(buf + n - 4, 0, 4);
    CHECK(!decodeOwner(buf, n, who2, seq)); // a number of 0 means "none" and is never sent

    CollarConfig cfg, cfg2;
    cfg.reportS = 300;
    n = encodeConfig(buf, sizeof(buf), cfg);
    CHECK(n == kConfigLength && hex(buf, n) == "02072c01");
    seq = 99;
    CHECK(decodeConfig(buf, n, cfg2, seq) && cfg2.reportS == 300 && seq == 0);
    n = encodeConfig(buf, sizeof(buf), cfg, 1763085600);
    CHECK(hex(buf, n) == "02072c01208d1669");
    CHECK(decodeConfig(buf, n, cfg2, seq) && cfg2.reportS == 300 && seq == 1763085600);
    buf[n] = 7; // later fields may follow the number
    CHECK(decodeConfig(buf, n + 1, cfg2, seq) && seq == 1763085600);
    CHECK(!decodeConfig(buf, n - 2, cfg2, seq)); // a cut-off number
    cfg.reportS = 10;
    // The home anchor, after the number.
    {
        CollarConfig a;
        a.reportS = 0;
        a.hasAnchor = true;
        a.anchor.kind = AnchorKind::Beacon;
        const uint8_t mac[6] = {0xAA, 0xBB, 0xCC, 0x01, 0x02, 0x03};
        memcpy(a.anchor.address, mac, 6);
        a.anchor.rssiDbm = -72;
        n = encodeConfig(buf, sizeof(buf), a, 77);
        CHECK(n == 2 + 2 + 4 + kAnchorLength);
        CHECK(hex(buf, n) == "020700004d000000" "01aabbcc010203b8");
        CollarConfig b;
        CHECK(decodeConfig(buf, n, b, seq) && seq == 77 && b.reportS == 0 && b.hasAnchor);
        CHECK(b.anchor.kind == AnchorKind::Beacon && memcmp(b.anchor.address, mac, 6) == 0 && b.anchor.rssiDbm == -72);
        CHECK(!b.hasGpsInside); // a base before 0.19.6: the collar keeps its own GPS pacing
        buf[n] = 3;
        CHECK(decodeConfig(buf, n + 3, b, seq) && b.hasAnchor && b.hasGpsInside && b.gpsInsideS == 3); // later fields after it
        CHECK(encodeConfig(buf, sizeof(buf), a, 0) == 4);      // no number, no anchor
        n = encodeConfig(buf, sizeof(buf), a, 77);
        buf[8] = 3; // no such kind
        CHECK(!decodeConfig(buf, n, b, seq));
        buf[8] = 2;
        buf[15] = static_cast<uint8_t>(-20); // threshold out of range
        CHECK(!decodeConfig(buf, n, b, seq));
        buf[15] = static_cast<uint8_t>(-100);
        CHECK(decodeConfig(buf, n, b, seq) && b.anchor.kind == AnchorKind::BaseRadio && b.anchor.rssiDbm == -100);
        CollarConfig plain;
        n = encodeConfig(buf, sizeof(buf), plain, 9);
        CHECK(decodeConfig(buf, n, b, seq) && !b.hasAnchor); // a base without home mode

        // The GPS interval inside the fences, after the anchor (0.19.6 on). Golden bytes shared
        // with the Python tests: the base radio at -75 dBm, interval 0, number 77, every 3 s.
        CollarConfig g;
        g.hasAnchor = true;
        g.anchor.kind = AnchorKind::BaseRadio;
        g.anchor.rssiDbm = -75;
        g.hasGpsInside = true;
        g.gpsInsideS = 3;
        n = encodeConfig(buf, sizeof(buf), g, 77);
        CHECK(n == 2 + 2 + 4 + kAnchorLength + 1);
        CHECK(hex(buf, n) == "020700004d000000" "02000000000000b5" "03");
        CHECK(decodeConfig(buf, n, b, seq) && b.hasGpsInside && b.gpsInsideS == 3 && b.anchor.rssiDbm == -75);
        buf[n - 1] = 0; // always on
        CHECK(decodeConfig(buf, n, b, seq) && b.hasGpsInside && b.gpsInsideS == 0);
        buf[n - 1] = kMaxGpsInsideS;
        CHECK(decodeConfig(buf, n, b, seq) && b.gpsInsideS == kMaxGpsInsideS);
        CHECK(!b.gpsSameEverywhere);
        buf[n - 1] = kMaxGpsInsideS + 1; // out of range: the whole message is refused
        CHECK(!decodeConfig(buf, n, b, seq));
        // Bit 7: the interval everywhere inside, whatever the distance to the bands.
        g.gpsSameEverywhere = true;
        n = encodeConfig(buf, sizeof(buf), g, 77);
        CHECK(hex(buf + n - 1, 1) == "83");
        CHECK(decodeConfig(buf, n, b, seq) && b.gpsInsideS == 3 && b.gpsSameEverywhere);
        buf[n - 1] = 0x80 | (kMaxGpsInsideS + 1);
        CHECK(!decodeConfig(buf, n, b, seq));
        g.gpsSameEverywhere = false;
        g.hasAnchor = false; // the interval only travels after the anchor
        CHECK(encodeConfig(buf, sizeof(buf), g, 77) == 8);
    }
    n = encodeConfig(buf, sizeof(buf), cfg, 5);
    CHECK(!decodeConfig(buf, n, cfg2, seq)); // below the shortest interval

    // The replay guard: numbers must rise; a message without one only until the first numbered one.
    CHECK(settingSeqAcceptable(0, 0) && settingSeqAcceptable(5, 0) && settingSeqAcceptable(6, 5));
    CHECK(!settingSeqAcceptable(5, 5) && !settingSeqAcceptable(4, 5) && !settingSeqAcceptable(0, 5));

    // Fences: older refused, the same set kept as it is, anything else loaded.
    {
        FenceSet cur, in;
        cur.version = 7;
        cur.zoneCount = 1;
        cur.zones[0] = square(ZoneKind::KeepIn, 2, 4, 6);
        in = cur;
        CHECK(fenceUpdate(7, in, cur) == FenceUpdate::Keep && sameFences(in, cur));
        CHECK(fenceUpdate(8, in, cur) == FenceUpdate::Load);
        CHECK(fenceUpdate(6, in, cur) == FenceUpdate::Refuse);
        in.zones[0].lonI[2] += 1;
        CHECK(!sameFences(in, cur) && fenceUpdate(7, in, cur) == FenceUpdate::Load); // same number, other fences
        in = cur;
        in.vibratePulseS = 3;
        CHECK(!sameFences(in, cur));
        in = cur;
        in.zones[0].warningM = 9;
        CHECK(!sameFences(in, cur));
        in = cur;
        in.zones[0].latI[20] = 5; // beyond the vertices in use: not part of the fence
        CHECK(sameFences(in, cur));
        FenceSet none;
        CHECK(fenceUpdate(0, none, none) == FenceUpdate::Load && fenceUpdate(3, in, none) == FenceUpdate::Load);
    }

    // Settings read back from flash are forced into range.
    {
        Settings bad;
        bad.maxShockLevel = 200;
        bad.shockLevel = 150;
        bad.vibrateLevel = 255;
        bad.maxShocksPerHour = 200;
        bad.lostConfirmFixes = 0;
        bad.rearmFixes = 0;
        bad.correctionMs = 0;
        bad.gpsFastSecs = 0;
        bad.gpsSlowSecs = 0;
        bad.gpsLostSecs = 0;
        bad.dogSpeedMps = 0;
        bad.minSats = 5; // what every state file before 0.19.11 holds
        sanitizeSettings(bad);
        CHECK(bad.minSats == kMinFixSats);
        CHECK(bad.maxShockLevel == 99 && bad.shockLevel == 99 && bad.vibrateLevel == 99 && bad.maxShocksPerHour == 16);
        CHECK(bad.lostConfirmFixes == 3 && bad.rearmFixes == 3 && bad.correctionMs == 1000 && bad.gpsFastSecs == 5 &&
              bad.gpsSlowSecs == 5 && bad.gpsLostSecs == 15 && bad.dogSpeedMps == 6);
        // Built in zeroed memory, so the padding between fields compares equal too.
        alignas(Settings) unsigned char goodMem[sizeof(Settings)] = {}, sameMem[sizeof(Settings)] = {};
        Settings &good = *new (goodMem) Settings, &same = *new (sameMem) Settings;
        sanitizeSettings(good);
        CHECK(std::memcmp(&good, &same, sizeof(good)) == 0); // the defaults pass through untouched
    }

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

static void testHomeWatch()
{
    // Readings every 2 s at -60 dBm (threshold -70), after a clear fix: home after 20 s.
    {
        HomeWatch h;
        h.setThreshold(-70);
        h.onFix(true, 1000);
        bool changed = false;
        uint32_t t = 2000;
        for (; t < 2000 + HomeWatch::kEnterMs; t += 2000) {
            h.onSighting(-60, t);
            changed = h.tick(t, true) || changed;
        }
        CHECK(!changed && !h.home()); // not yet: the run is just short of 20 s
        h.onSighting(-60, t);
        CHECK(h.tick(t, true) && h.home());
        CHECK(!h.tick(t + 500, true)); // reported once
        // Weaker but within the margin: stays home.
        h.onSighting(-74, t + 2000);
        CHECK(!h.tick(t + 2000 + HomeWatch::kLeaveMs, true) && h.home());
        // Then nothing strong enough for longer than kLeaveMs: away.
        h.onSighting(-80, t + 4000);
        CHECK(h.tick(t + 2000 + HomeWatch::kLeaveMs + 1, true) && !h.home());
        // Back near within 10 minutes: home again without a new fix (the anchor kept proving it).
        const uint32_t back = t + 60000;
        for (uint32_t u = back; u <= back + HomeWatch::kEnterMs; u += 2000) {
            h.onSighting(-60, u);
            h.tick(u, true);
        }
        CHECK(h.home());
        // A fix in a band ends home at once.
        h.onFix(false, back + 30000);
        CHECK(h.tick(back + 30000, true) && !h.home());
        // And then the anchor alone cannot bring it back: the newest fix was not clear.
        for (uint32_t u = back + 32000; u <= back + 32000 + 2 * HomeWatch::kEnterMs; u += 2000) {
            h.onSighting(-55, u);
            h.tick(u, true);
        }
        CHECK(!h.home());
    }
    // No clear fix at all, or an old one: never home.
    {
        HomeWatch h;
        h.setThreshold(-70);
        for (uint32_t t = 0; t <= 60000; t += 2000) {
            h.onSighting(-50, t);
            h.tick(t, true);
        }
        CHECK(!h.home());
        h.onFix(true, 100000);
        const uint32_t late = 100000 + HomeWatch::kFreshMs + 1000;
        for (uint32_t t = late; t <= late + 60000; t += 2000) {
            h.onSighting(-50, t);
            h.tick(t, true);
        }
        CHECK(!h.home());
    }
    // One weak reading restarts the 20 s; a gap longer than kLeaveMs does too.
    {
        HomeWatch h;
        h.setThreshold(-70);
        h.onFix(true, 0);
        for (uint32_t t = 2000; t <= 18000; t += 2000) {
            h.onSighting(-60, t);
            h.tick(t, true);
        }
        h.onSighting(-71, 20000);
        h.onSighting(-60, 22000);
        CHECK(!h.tick(30000, true));
        h.onSighting(-60, 40000); // gap of 18 s
        h.onSighting(-60, 42000);
        CHECK(!h.tick(42000, true) && !h.home());
        for (uint32_t t = 44000; t <= 62000; t += 2000) {
            h.onSighting(-60, t);
            h.tick(t, true);
        }
        CHECK(h.home()); // 40 s to 60 s: 20 s unbroken
    }
    // Not allowed (lost, no fences, no anchor): never home, and home ends at once.
    {
        HomeWatch h;
        h.setThreshold(-70);
        h.onFix(true, 0);
        for (uint32_t t = 2000; t <= 30000; t += 2000) {
            h.onSighting(-60, t);
            h.tick(t, false);
        }
        CHECK(!h.home());
        for (uint32_t t = 32000; t <= 54000; t += 2000) {
            h.onSighting(-60, t);
            h.tick(t, true);
        }
        CHECK(h.home());
        CHECK(h.tick(55000, false) && !h.home());
    }
    // A reset (new fences or a new anchor) forgets the clear fix.
    {
        HomeWatch h;
        h.setThreshold(-70);
        h.onFix(true, 0);
        for (uint32_t t = 2000; t <= 24000; t += 2000) {
            h.onSighting(-60, t);
            h.tick(t, true);
        }
        CHECK(h.home());
        h.reset();
        CHECK(h.tick(25000, true) && !h.home());
        for (uint32_t t = 26000; t <= 60000; t += 2000) {
            h.onSighting(-60, t);
            h.tick(t, true);
        }
        CHECK(!h.home());
    }
    // The millisecond clock wrapping does not matter.
    {
        HomeWatch h;
        h.setThreshold(-70);
        const uint32_t base = 0xFFFFFFFFu - 10000;
        h.onFix(true, base);
        for (uint32_t i = 1; i <= 12; i++) {
            h.onSighting(-60, base + i * 2000);
            h.tick(base + i * 2000, true);
        }
        CHECK(h.home());
    }
}

static void testBeacon()
{
    LostBeacon b;
    const uint32_t t0 = 1000;
    b.onFix(true, t0);
    CHECK(!b.active());
    b.tick(t0 + LostBeacon::kTriggerMs - 1);
    CHECK(!b.active());
    b.tick(t0 + LostBeacon::kTriggerMs); // no new fix needed: 90 s out is enough
    CHECK(b.active());
    const uint32_t on = t0 + LostBeacon::kTriggerMs;
    CHECK(b.bleOn(on) && b.bleOn(on + LostBeacon::kBleOnMs - 1));
    CHECK(!b.bleOn(on + LostBeacon::kBleOnMs) && !b.bleOn(on + LostBeacon::kBleOnMs + LostBeacon::kBleOffMs - 1));
    CHECK(b.bleOn(on + LostBeacon::kBleOnMs + LostBeacon::kBleOffMs));
    CHECK(b.meshDue(on));
    CHECK(!b.meshDue(on + 1000));
    CHECK(!b.meshDue(on + LostBeacon::kMeshEveryMs - 1));
    CHECK(b.meshDue(on + LostBeacon::kMeshEveryMs));
    // Back inside: stops.
    b.onFix(false, on + 400000);
    CHECK(!b.active() && !b.bleOn(on + 400000) && !b.meshDue(on + 400000));
    // A short trip out does not trigger it.
    b.onFix(true, 2000000);
    b.onFix(false, 2000000 + 60000);
    b.tick(2000000 + 200000);
    CHECK(!b.active());
    // Silenced: stays off while out, re-arms after the dog has been back inside.
    b.onFix(true, 3000000);
    b.tick(3000000 + LostBeacon::kTriggerMs);
    CHECK(b.active());
    b.silence();
    CHECK(!b.active() && b.silenced());
    b.onFix(true, 3000000 + 10 * LostBeacon::kTriggerMs);
    CHECK(!b.active());
    b.onFix(false, 4000000);
    CHECK(!b.silenced());
    b.onFix(true, 4100000);
    b.tick(4100000 + LostBeacon::kTriggerMs);
    CHECK(b.active());

    // GPS: half as often as lost mode.
    Settings s;
    CHECK(gpsIntervalSecs(s, CollarState::Lost, -50, 10, true) == kLostGpsSecs); // once a minute, beacon or not

    char name[30];
    CHECK(beaconName("Rex", "(555) 123-4567", name, sizeof(name)) == strlen("Rex is lost 555123-4567"));
    CHECK(strcmp(name, "Rex is lost 555123-4567") == 0);
    beaconName("Maximilian Barksworth", "+44 7700 900123", name, sizeof(name));
    CHECK(strlen(name) <= 29 && strncmp(name, "Maximilian ", 11) == 0 && strstr(name, " lost +447700900123"));
    beaconName("Rex", "", name, sizeof(name));
    CHECK(strcmp(name, "Rex is lost") == 0);
    char small[27];
    beaconName("Rex", "5551234567", small, sizeof(small)); // primary advertisement: 26 bytes
    CHECK(strcmp(small, "Rex is lost 5551234567") == 0);

    char text[200];
    beaconText("Rex", "Ken", "555-123-4567", 401234567, -751234567, text, sizeof(text));
    CHECK(strcmp(text, "Please help get Rex home. Call Ken 555-123-4567. Last seen 40.12345,-75.12345") == 0);
    beaconText("Rex", "", "", 0, 0, text, sizeof(text));
    CHECK(strcmp(text, "Please help get Rex home.") == 0);

    using namespace proto;
    OwnerInfo o, back;
    strcpy(o.dog, "Rex");
    strcpy(o.owner, "Ken");
    strcpy(o.phone, "555-123-4567");
    uint8_t buf[100];
    uint32_t oseq = 0;
    size_t n = encodeOwner(buf, sizeof(buf), o);
    CHECK(n == 2 + 1 + 3 + 1 + 3 + 1 + 12);
    CHECK(hex(buf, n) == "0205035265780" "34b656e0c3535352d3132332d34353637");
    MsgType t;
    CHECK(messageType(buf, n, t) && t == MsgType::Owner);
    CHECK(decodeOwner(buf, n, back, oseq) && strcmp(back.dog, "Rex") == 0 && strcmp(back.phone, "555-123-4567") == 0);
    CHECK(encodeHello(buf, sizeof(buf), true, false, 87) == kHelloSize && hex(buf, kHelloSize) == "02060157");
    CHECK(messageType(buf, kHelloSize, t) && t == MsgType::Hello);
    n = encodeOwner(buf, sizeof(buf), o);
    buf[3] = '\n'; // control characters are refused
    CHECK(!decodeOwner(buf, n, back, oseq));
    CHECK(!decodeOwner(buf, n - 1, back, oseq));

    // Collar settings: how often to report.
    CollarConfig cfg, cback;
    cfg.reportS = 300;
    n = encodeConfig(buf, sizeof(buf), cfg);
    CHECK(n == kConfigLength && hex(buf, n) == "02072c01");
    CHECK(messageType(buf, n, t) && t == MsgType::Config);
    CHECK(decodeConfig(buf, n, cback, oseq) && cback.reportS == 300);
    buf[n] = 9;
    CHECK(!decodeConfig(buf, n + 1, cback, oseq)); // one stray byte is not a sequence number
    CHECK(!decodeConfig(buf, n - 1, cback, oseq));
    cfg.reportS = 0; // the default
    n = encodeConfig(buf, sizeof(buf), cfg);
    CHECK(decodeConfig(buf, n, cback, oseq) && cback.reportS == 0);
    cfg.reportS = kMinReportS - 1; // too often
    n = encodeConfig(buf, sizeof(buf), cfg);
    CHECK(!decodeConfig(buf, n, cback, oseq));
    cfg.reportS = kMaxReportS + 1;
    n = encodeConfig(buf, sizeof(buf), cfg);
    CHECK(!decodeConfig(buf, n, cback, oseq));

    // The collar's Meshtastic names follow the dog's name.
    char ln[40], sn[5];
    meshNames("Rex", ln, sizeof(ln), sn, sizeof(sn));
    CHECK(strcmp(ln, "Rex") == 0 && strcmp(sn, "REX") == 0);
    meshNames("  Biscuit  ", ln, sizeof(ln), sn, sizeof(sn));
    CHECK(strcmp(ln, "Biscuit") == 0 && strcmp(sn, "BISC") == 0);
    meshNames("Old Yeller the Dog Star", ln, sizeof(ln), sn, sizeof(sn));
    CHECK(strcmp(ln, "Old Yeller the Dog Star") == 0 && strcmp(sn, "OYTD") == 0);
    meshNames("Zo\xc3\xab", ln, sizeof(ln), sn, sizeof(sn)); // "Zoe" with a diaeresis: 4 bytes
    CHECK(strcmp(ln, "Zo\xc3\xab") == 0 && strcmp(sn, "ZO\xc3\xab") == 0);
    meshNames("Chlo\xc3\xa9", ln, sizeof(ln), sn, sizeof(sn)); // the accent doesn't fit whole: cut before it
    CHECK(strcmp(sn, "CHLO") == 0);
    meshNames("Bl\xc3\xa9", ln, 4, sn, sizeof(sn)); // a long name cut on a character boundary
    CHECK(strcmp(ln, "Bl") == 0);
}

// --------------------------------------------------------------------------------------------
// The companion app's link: SHA-256 and HMAC, the key list from the base, and the session
// --------------------------------------------------------------------------------------------

static void testSha256()
{
    using namespace hash;
    uint8_t d[kSha256Len];
    Sha256 a;
    a.update((const uint8_t *)"abc", 3);
    a.finish(d);
    CHECK(hex(d, sizeof(d)) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    Sha256 e;
    e.finish(d);
    CHECK(hex(d, sizeof(d)) == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
    // Fed in uneven pieces across block boundaries.
    std::string big(1000, 'a');
    Sha256 b;
    for (size_t i = 0; i < big.size(); i += 37)
        b.update((const uint8_t *)big.data() + i, big.size() - i < 37 ? big.size() - i : 37);
    b.finish(d);
    CHECK(hex(d, sizeof(d)) == "41edece42d63e8d9bf515a9ba6932e1c20cbc9f5a5d134645adb5db1b9737ea3");
    // RFC 4231 test cases 2 and 6 (a key longer than a block).
    const char *msg = "what do ya want for nothing?";
    hmacSha256((const uint8_t *)"Jefe", 4, (const uint8_t *)msg, strlen(msg), d);
    CHECK(hex(d, sizeof(d)) == "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843");
    uint8_t longKey[131];
    memset(longKey, 0xaa, sizeof(longKey));
    msg = "Test Using Larger Than Block-Size Key - Hash Key First";
    hmacSha256(longKey, sizeof(longKey), (const uint8_t *)msg, strlen(msg), d);
    CHECK(hex(d, sizeof(d)) == "60e431591ee0b67f0d8a26aacbf5b77f8e0bc6213728c5140546040f0ee37f54");
    CHECK(sameBytes(d, d, sizeof(d)));
    uint8_t other[kSha256Len];
    memcpy(other, d, sizeof(d));
    other[31] ^= 1;
    CHECK(!sameBytes(d, other, sizeof(d)));
}

static void testBaseLink()
{
    // In reach until shown otherwise, with no reports sent at all (no base yet) however long.
    BaseLink l;
    CHECK(l.reachable());
    CHECK(!l.tick(0));
    CHECK(!l.tick(100000000) && l.reachable());

    // Reports answered: in reach.
    const uint32_t t = 1000000;
    for (uint32_t i = 0; i < 10; i++) {
        l.onSent(t + i * 30000);
        l.onHeard();
        CHECK(!l.tick(t + i * 30000 + 1000) && l.reachable());
    }

    // Three unanswered reports are not enough on their own: the base has 2 minutes from the first.
    const uint32_t u = 2000000;
    l.onSent(u);
    l.onSent(u + 30000);
    l.onSent(u + 60000);
    CHECK(!l.tick(u + 60000) && l.reachable());
    CHECK(!l.tick(u + BaseLink::kSilentMs - 1) && l.reachable());
    CHECK(l.tick(u + BaseLink::kSilentMs) && !l.reachable());
    CHECK(!l.tick(u + BaseLink::kSilentMs + 1000) && !l.reachable()); // the change is told once

    // Nor is a long silence after fewer reports: inside, a report goes every 10 minutes.
    BaseLink q;
    q.onSent(0);
    q.onSent(600000);
    CHECK(!q.tick(1000000) && q.reachable());
    q.onSent(1200000);
    CHECK(q.tick(1200000) && !q.reachable()); // 20 minutes since the first: out of reach

    // Anything from the base brings it back at once, and starts the count again.
    l.onSent(u + 150000);
    l.onHeard();
    CHECK(l.reachable());
    CHECK(l.tick(u + 151000) && l.reachable());
    CHECK(!l.tick(u + 152000));
    l.onSent(u + 200000);
    l.onSent(u + 230000);
    CHECK(!l.tick(u + 1000000) && l.reachable()); // two unanswered: still in reach
    l.onSent(u + 1000000);
    CHECK(l.tick(u + 1000000) && !l.reachable());
    // Heard twice in a row while in reach: no change to report.
    l.onHeard();
    l.onHeard();
    CHECK(l.tick(u + 1001000) && l.reachable());

    // millis() wrapping round while out of reach changes nothing.
    BaseLink w;
    const uint32_t near = UINT32_MAX - 60000;
    w.onSent(near);
    w.onSent(near + 30000);
    w.onSent(near + 60000); // wrapped
    CHECK(!w.tick(near + 90000) && w.reachable());
    CHECK(w.tick(near + BaseLink::kSilentMs) && !w.reachable());

    // Reports: the owner's interval; while lost, at least every 30 s while the base answers, and
    // a check for the base every 5 minutes, with the plea, while it does not.
    CHECK(kLostProbeMs == LostBeacon::kMeshEveryMs && kLostProbeMs == 5 * 60 * 1000);
    CHECK(reportIntervalMs(600000, 30000, false, true) == 600000);
    CHECK(reportIntervalMs(600000, 30000, false, false) == 600000); // not lost: the base makes no difference
    CHECK(reportIntervalMs(600000, 30000, true, true) == 30000);
    CHECK(reportIntervalMs(20000, 30000, true, true) == 20000); // the owner's own is quicker
    CHECK(reportIntervalMs(600000, 30000, true, false) == kLostProbeMs);
    CHECK(reportIntervalMs(20000, 30000, true, false) == kLostProbeMs);

    // A lost collar out of reach: one check every 5 minutes keeps it out of reach while nothing
    // answers, and the first answer brings it back at once.
    BaseLink lost;
    const uint32_t v = 3000000;
    for (uint32_t i = 0; i < 5; i++)
        lost.onSent(v + i * 30000); // the 30 s reports while lost
    CHECK(lost.tick(v + 4 * 30000) && !lost.reachable());
    for (uint32_t i = 1; i <= 6; i++) {
        lost.onSent(v + i * kLostProbeMs);
        CHECK(!lost.tick(v + i * kLostProbeMs + 30000) && !lost.reachable());
    }
    lost.onHeard(); // the acknowledgement of the 6th check
    CHECK(lost.tick(v + 6 * kLostProbeMs + 31000) && lost.reachable());

    // The GPS: a lost dog gets a fix every minute while its position can be passed on, and none
    // (the GPS powered down) while it cannot, beacon or not.
    Settings s;
    CHECK(gpsIntervalSecs(s, CollarState::Lost, -50, 10, false, true) == 60);
    CHECK(gpsIntervalSecs(s, CollarState::Lost, -50, 10, true, true) == 60);
    CHECK(gpsIntervalSecs(s, CollarState::Lost, -50, 10, false, false) == kLostOffGpsSecs);
    CHECK(gpsIntervalSecs(s, CollarState::Lost, -50, 10, true, false) == kLostOffGpsSecs);
    CHECK(gpsIntervalSecs(s, CollarState::Outside, -5, 10, true, false) == kLostOffGpsSecs); // beacon on, not yet confirmed
    CHECK(gpsIntervalSecs(s, CollarState::Outside, -5, 10, true, true) == 60);
    // Inside the fences, near a line or not yet confirmed outside, the base makes no difference:
    // the collar enforces the fence on its own.
    CHECK(gpsIntervalSecs(s, CollarState::Inside, 1000, 10, false, false) == kFarGpsSecs);
    CHECK(gpsIntervalSecs(s, CollarState::Inside, 20, 10, false, false) == 5);
    CHECK(gpsIntervalSecs(s, CollarState::Warning, 500, 10, false, false) == 5);
    CHECK(gpsIntervalSecs(s, CollarState::Outside, -5, 10, false, false) == 5);
    // Pacing off or no fences: the owner's interval, as before.
    CHECK(gpsIntervalSecs(s, CollarState::NoFence, 500, 10, false, false) == 0);
    s.gpsPacing = false;
    CHECK(gpsIntervalSecs(s, CollarState::Lost, -50, 10, false, false) == 0);
    // The old lost-mode setting no longer counts.
    s.gpsPacing = true;
    s.gpsLostSecs = 400;
    CHECK(gpsIntervalSecs(s, CollarState::Lost, -50, 10, false, true) == 60);
    // The owner's GPS interval inside the fences: that often within 35 m of the outermost band;
    // from 35 to 100 m rising with the distance to 120 s; beyond 100 m every 180 s.
    Settings in;
    CHECK(setGpsInsideSecs(in, 3) && !setGpsInsideSecs(in, 3));
    CHECK(in.gpsFastSecs == 3);
    CHECK(gpsIntervalSecs(in, CollarState::Inside, 12, 10) == 3);        // 2 m from the band
    CHECK(gpsIntervalSecs(in, CollarState::Inside, 10 + 35, 10) == 3);   // 35 m: still near
    CHECK(gpsIntervalSecs(in, CollarState::Inside, 10 + 36, 10) == 4);   // just past: a little slower
    CHECK(gpsIntervalSecs(in, CollarState::Inside, 10 + 67.5f, 10) == 61); // halfway: 3 + 117 / 2
    CHECK(gpsIntervalSecs(in, CollarState::Inside, 10 + 100, 10) == 120);
    CHECK(gpsIntervalSecs(in, CollarState::Inside, 10 + 101, 10) == 180);
    CHECK(gpsIntervalSecs(in, CollarState::Inside, 5000, 10) == 180);
    uint32_t prev = 0; // never faster further in
    for (int m = 0; m <= 300; m++) {
        const uint32_t v = gpsIntervalSecs(in, CollarState::Inside, 10.0f + m, 10);
        CHECK(v >= prev);
        prev = v;
    }
    CHECK(gpsIntervalSecs(in, CollarState::Inside, 0, 10) == 3);         // a rejected fix: fast
    CHECK(gpsIntervalSecs(in, CollarState::Warning, 5, 10) == 3);
    CHECK(gpsIntervalSecs(in, CollarState::Outside, -5, 10) == 3);
    CHECK(gpsIntervalSecs(in, CollarState::Lost, -50, 10) == kLostGpsSecs); // lost mode keeps its own pace
    CHECK(gpsSettingSecs(gpsIntervalSecs(in, CollarState::Inside, 12, 10)) == 3); // GPS always on
    CHECK(gpsSettingSecs(kFarGpsSecs) == kFarGpsSecs - 20);
    setGpsInsideSecs(in, 0); // always on near the band: a fix every second
    CHECK(in.gpsFastSecs == 1 && gpsIntervalSecs(in, CollarState::Inside, 12, 10) == 1);
    CHECK(gpsIntervalSecs(in, CollarState::Inside, 10 + 100, 10) == 120);
    setGpsInsideSecs(in, 30);
    CHECK(gpsIntervalSecs(in, CollarState::Inside, 40, 10) == 30);
    CHECK(gpsIntervalSecs(in, CollarState::Inside, 10 + 36, 10) >= 30);
    CHECK(gpsIntervalSecs(in, CollarState::Inside, 1000, 10) == 180);
    CHECK(gpsIntervalSecs(in, CollarState::Warning, 5, 10) == 30);
    CHECK(gpsIntervalSecs(in, CollarState::Outside, -5, 10) == kOutsideGpsSecs); // past the line: at least every 5 s
    Settings saved = in;
    sanitizeSettings(saved); // what a restart reads back keeps it
    CHECK(saved.gpsFastSecs == 30);
    // The owner's override: that interval wherever the dog is inside, but never once it is past a
    // line or lost.
    setGpsInsideSecs(in, 3);
    CHECK(gpsIntervalSecs(in, CollarState::Inside, 5000, 10, false, true, true) == 3);
    CHECK(gpsIntervalSecs(in, CollarState::Inside, 10 + 60, 10, false, true, true) == 3);
    CHECK(gpsIntervalSecs(in, CollarState::Warning, 5, 10, false, true, true) == 3);
    CHECK(gpsIntervalSecs(in, CollarState::Lost, -50, 10, false, true, true) == kLostGpsSecs);
    CHECK(gpsIntervalSecs(in, CollarState::Lost, -50, 10, true, true, true) == kLostGpsSecs);
    CHECK(gpsIntervalSecs(in, CollarState::Lost, -50, 10, false, false, true) == kLostOffGpsSecs);
    setGpsInsideSecs(in, 30);
    CHECK(gpsIntervalSecs(in, CollarState::Inside, 5000, 10, false, true, true) == 30);
    CHECK(gpsIntervalSecs(in, CollarState::Outside, -5, 10, false, true, true) == kOutsideGpsSecs);

    // A collar the base never sent it: the same steps from 5 s.
    Settings old;
    CHECK(gpsIntervalSecs(old, CollarState::Inside, 10 + 30, 10) == 5);
    CHECK(gpsIntervalSecs(old, CollarState::Inside, 10 + 200, 10) == 180);

    // What Meshtastic is given, with its hold after each lock, for a fix every minute.
    CHECK(gpsSettingSecs(kLostGpsSecs) == kLostGpsSecs - 20);
}

static void testAppLink()
{
    using namespace proto;
    uint8_t buf[64], reply[64];
    size_t n = 0, rn = 0;

    // The list of authorized phones, as the base sends it. The same bytes are in the base's and
    // the app's tests.
    AppKeySet keys;
    keys.count = 1;
    keys.keys[0].id = 0x1a2b3c4d;
    for (size_t i = 0; i < kAppKeyLen; i++)
        keys.keys[0].key[i] = static_cast<uint8_t>(i);
    n = encodeAppKeys(buf, sizeof(buf), keys, 1763085600);
    CHECK(hex(buf, n) == "0208208d1669014d3c2b1a000102030405060708090a0b0c0d0e0f");
    MsgType t;
    CHECK(messageType(buf, n, t) && t == MsgType::AppKeys);
    AppKeySet back;
    uint32_t seq = 0;
    CHECK(decodeAppKeys(buf, n, back, seq) && seq == 1763085600 && back.count == 1 && back.keys[0].id == 0x1a2b3c4d &&
          memcmp(back.keys[0].key, keys.keys[0].key, kAppKeyLen) == 0);
    CHECK(!decodeAppKeys(buf, n - 1, back, seq)); // cut short
    buf[6] = 2;
    CHECK(!decodeAppKeys(buf, n, back, seq)); // the count does not match the length
    buf[6] = 1;
    buf[2] = buf[3] = buf[4] = buf[5] = 0;
    CHECK(!decodeAppKeys(buf, n, back, seq)); // no sequence number
    AppKeySet twice = keys;
    twice.count = 2;
    twice.keys[1] = keys.keys[0];
    n = encodeAppKeys(buf, sizeof(buf), twice, 5);
    CHECK(n && !decodeAppKeys(buf, n, back, seq)); // the same phone twice
    twice.keys[1].id = 0;
    n = encodeAppKeys(buf, sizeof(buf), twice, 5);
    CHECK(!decodeAppKeys(buf, n, back, seq)); // phone number 0
    AppKeySet none;
    n = encodeAppKeys(buf, sizeof(buf), none, 9);
    CHECK(n == kAppKeysHeader && decodeAppKeys(buf, n, back, seq) && back.count == 0); // every phone removed
    AppKeySet full;
    full.count = kMaxAppKeys;
    for (size_t i = 0; i < kMaxAppKeys; i++)
        full.keys[i].id = static_cast<uint32_t>(i + 1);
    uint8_t big[kAppKeysMax];
    n = encodeAppKeys(big, sizeof(big), full, 9);
    CHECK(n == kAppKeysMax && decodeAppKeys(big, n, back, seq) && back.count == kMaxAppKeys);
    full.count = kMaxAppKeys + 1;
    CHECK(encodeAppKeys(big, sizeof(big), full, 9) == 0);

    n = encodeAppKeysAck(buf, sizeof(buf), 1763085600, 1);
    CHECK(hex(buf, n) == "0209208d166901");
    uint8_t count = 0;
    CHECK(decodeAppKeysAck(buf, n, seq, count) && seq == 1763085600 && count == 1);
    CHECK(messageType(buf, n, t) && t == MsgType::AppKeysAck);

    // What a phone may ask for.
    CHECK(appActionAllowed(CommandAction::SilenceLost) && appActionAllowed(CommandAction::RequestStatus));
    CHECK(appActionAllowed(CommandAction::Beep) && appActionAllowed(CommandAction::Vibrate));
    CHECK(appActionAllowed(CommandAction::CorrectionsOff) && appActionAllowed(CommandAction::CorrectionsOn));
    CHECK(!appActionAllowed(CommandAction::Shock) && !appActionAllowed(CommandAction::ShockOn));
    CHECK(!appActionAllowed(CommandAction::ForceLost) && !appActionAllowed(CommandAction::ClearLost));
    CHECK(!appActionAllowed(static_cast<CommandAction>(99)));

    // A session, byte for byte.
    uint8_t appNonce[kAppNonceLen], collarNonce[kAppNonceLen];
    for (size_t i = 0; i < kAppNonceLen; i++) {
        appNonce[i] = static_cast<uint8_t>(0xa0 + i);
        collarNonce[i] = static_cast<uint8_t>(0xc0 + i);
    }
    AppLink link;
    AppRequest req;
    n = encodeAppHello(buf, sizeof(buf), 0x1a2b3c4d, appNonce);
    CHECK(hex(buf, n) == "02104d3c2b1aa0a1a2a3a4a5a6a7");
    CHECK(messageType(buf, n, t) && t == MsgType::AppHello);
    CHECK(link.handle(buf, n, keys, collarNonce, reply, sizeof(reply), rn, req) == AppLink::Outcome::Reply);
    CHECK(hex(reply, rn) == "02114d3c2b1ac0c1c2c3c4c5c6c7c66cfaa9f4fe36cb");
    CHECK(link.active());
    uint8_t sk[kAppSessionKeyLen];
    appSessionKey(keys.keys[0], appNonce, collarNonce, sk);
    CHECK(hex(sk, sizeof(sk)) == "4b4ad99bb6e9aa4a581e9cc9defdb082abb994605cae8f691fde30c639488fce");

    AppRequest ask;
    ask.keyId = 0x1a2b3c4d;
    ask.counter = 1;
    ask.action = CommandAction::SilenceLost;
    n = encodeAppRequest(buf, sizeof(buf), ask, sk);
    CHECK(hex(buf, n) == "02124d3c2b1a010000000f000000d2b8917361311478");
    CHECK(link.handle(buf, n, keys, collarNonce, reply, sizeof(reply), rn, req) == AppLink::Outcome::Request);
    CHECK(req.keyId == 0x1a2b3c4d && req.counter == 1 && req.action == CommandAction::SilenceLost);
    StatusReport s{3, 4, 87, 400001234, -750004321, 180, 9, -12, 1};
    rn = link.status(reply, sizeof(reply), req, AppResult::Done, s, 42);
    CHECK(rn == kAppStatusLength);
    CHECK(hex(reply, rn) == "02134d3c2b1a010000000003000457d288d7179fd74bd3b40009f4ff012a0017682af07f6aecce");

    // The same request again is a replay: refused, and the app is told to start over.
    CHECK(link.handle(buf, n, keys, collarNonce, reply, sizeof(reply), rn, req) == AppLink::Outcome::Reply);
    CHECK(hex(reply, rn) == "02144d3c2b1a02");
    // A later counter is fine, and counters may skip.
    ask.counter = 5;
    ask.action = CommandAction::Vibrate;
    ask.intensity = 40;
    ask.durationMs = 1500;
    n = encodeAppRequest(buf, sizeof(buf), ask, sk);
    CHECK(link.handle(buf, n, keys, collarNonce, reply, sizeof(reply), rn, req) == AppLink::Outcome::Request);
    CHECK(req.action == CommandAction::Vibrate && req.intensity == 40 && req.durationMs == 1500 && req.counter == 5);
    ask.counter = 4; // older than one already used
    n = encodeAppRequest(buf, sizeof(buf), ask, sk);
    CHECK(link.handle(buf, n, keys, collarNonce, reply, sizeof(reply), rn, req) == AppLink::Outcome::Reply);

    // One changed bit anywhere and the signature no longer fits. A bad request does not use up
    // its counter, so the real one still goes through afterwards.
    ask.counter = 6;
    ask.action = CommandAction::Beep;
    n = encodeAppRequest(buf, sizeof(buf), ask, sk);
    for (size_t i = 0; i < n; i++) {
        uint8_t bad[64];
        memcpy(bad, buf, n);
        bad[i] ^= 0x01;
        CHECK(link.handle(bad, n, keys, collarNonce, reply, sizeof(reply), rn, req) != AppLink::Outcome::Request);
    }
    CHECK(link.handle(buf, n, keys, collarNonce, reply, sizeof(reply), rn, req) == AppLink::Outcome::Request);
    CHECK(link.handle(buf, n - 1, keys, collarNonce, reply, sizeof(reply), rn, req) == AppLink::Outcome::Ignore);

    // A new hello starts a new session with a new collar nonce: requests signed for the old
    // session (a recording, say) are refused even with a counter the new session has not seen.
    ask.counter = 7;
    n = encodeAppRequest(buf, sizeof(buf), ask, sk); // signed with the old session key
    uint8_t hello[kAppHelloLength];
    encodeAppHello(hello, sizeof(hello), 0x1a2b3c4d, appNonce);
    uint8_t collarNonce2[kAppNonceLen] = {1, 2, 3, 4, 5, 6, 7, 8};
    CHECK(link.handle(hello, sizeof(hello), keys, collarNonce2, reply, sizeof(reply), rn, req) == AppLink::Outcome::Reply);
    CHECK(reply[1] == static_cast<uint8_t>(MsgType::AppChallenge) && memcmp(reply + 6, collarNonce2, kAppNonceLen) == 0);
    CHECK(link.handle(buf, n, keys, collarNonce2, reply, sizeof(reply), rn, req) == AppLink::Outcome::Reply);
    CHECK(reply[1] == static_cast<uint8_t>(MsgType::AppError) && reply[6] == static_cast<uint8_t>(AppErrorCode::NoSession));
    uint8_t sk2[kAppSessionKeyLen];
    appSessionKey(keys.keys[0], appNonce, collarNonce2, sk2);
    CHECK(memcmp(sk, sk2, sizeof(sk)) != 0);
    ask.counter = 1; // counters start again with the session
    n = encodeAppRequest(buf, sizeof(buf), ask, sk2);
    CHECK(link.handle(buf, n, keys, collarNonce2, reply, sizeof(reply), rn, req) == AppLink::Outcome::Request);

    // A phone the base never authorized, or one with the right number and the wrong key.
    encodeAppHello(hello, sizeof(hello), 0x99999999, appNonce);
    CHECK(link.handle(hello, sizeof(hello), keys, collarNonce, reply, sizeof(reply), rn, req) == AppLink::Outcome::Reply);
    CHECK(hex(reply, rn) == "021499999999" "01");
    CHECK(link.active()); // a stranger's hello does not end the owner's session
    AppKey wrong = keys.keys[0];
    wrong.key[0] ^= 0xff;
    uint8_t skWrong[kAppSessionKeyLen];
    appSessionKey(wrong, appNonce, collarNonce2, skWrong);
    ask.counter = 2;
    n = encodeAppRequest(buf, sizeof(buf), ask, skWrong);
    CHECK(link.handle(buf, n, keys, collarNonce2, reply, sizeof(reply), rn, req) == AppLink::Outcome::Reply);

    // The base removes the phone: its session ends at once and it cannot start another.
    n = encodeAppRequest(buf, sizeof(buf), ask, sk2);
    link.keysChanged(keys); // nothing changed for this phone: the session goes on
    CHECK(link.active());
    link.keysChanged(none);
    CHECK(!link.active());
    CHECK(link.handle(buf, n, none, collarNonce2, reply, sizeof(reply), rn, req) == AppLink::Outcome::Reply);
    CHECK(reply[6] == static_cast<uint8_t>(AppErrorCode::UnknownKey));
    CHECK(link.status(reply, sizeof(reply), req, AppResult::Done, s, 0) == 0);
    encodeAppHello(hello, sizeof(hello), 0x1a2b3c4d, appNonce);
    CHECK(link.handle(hello, sizeof(hello), none, collarNonce, reply, sizeof(reply), rn, req) == AppLink::Outcome::Reply);
    CHECK(reply[1] == static_cast<uint8_t>(MsgType::AppError));
    // The phone's key is replaced by another under the same number: the old session ends too.
    CHECK(link.handle(hello, sizeof(hello), keys, collarNonce, reply, sizeof(reply), rn, req) == AppLink::Outcome::Reply);
    CHECK(link.active());
    AppKeySet changed = keys;
    changed.keys[0].key[5] ^= 1;
    link.keysChanged(changed);
    CHECK(!link.active());

    // Other GeoCollar messages are none of the app link's business.
    Command c{CommandAction::Beep, 0, 500, 1};
    n = encodeCommand(buf, sizeof(buf), c);
    CHECK(link.handle(buf, n, keys, collarNonce, reply, sizeof(reply), rn, req) == AppLink::Outcome::Ignore);
    CHECK(link.handle(buf, 1, keys, collarNonce, reply, sizeof(reply), rn, req) == AppLink::Outcome::Ignore);
    // No room for the answer: nothing is written.
    CHECK(link.handle(hello, sizeof(hello), keys, collarNonce, reply, 4, rn, req) == AppLink::Outcome::Ignore && rn == 0);
}

// --------------------------------------------------------------------------------------------
// A collar walked through every zone
// --------------------------------------------------------------------------------------------

/// A rectangle from (south, west) to (north, east), in metres from the test origin.
static Zone box(ZoneKind kind, double south, double west, double north, double east, uint8_t danger, uint8_t warning,
                uint8_t attention)
{
    Zone z;
    z.kind = kind;
    z.dangerM = danger;
    z.warningM = warning;
    z.attentionM = attention;
    z.vertexCount = 4;
    z.latI[0] = latAt(south);
    z.lonI[0] = lonAt(west);
    z.latI[1] = latAt(south);
    z.lonI[1] = lonAt(east);
    z.latI[2] = latAt(north);
    z.lonI[2] = lonAt(east);
    z.latI[3] = latAt(north);
    z.lonI[3] = lonAt(west);
    return z;
}

/// The collar's pieces wired together the way GeoCollarModule wires them, one second at a time:
/// a GPS fix at the paced interval (none while home, and one a second after a reading that
/// started a second look), each fed to the fence engine and, if good enough to use, to the lost
/// beacon and home mode; the anchor heard every 2 s while the dog is within its range.
struct WalkingCollar {
    FenceEngine engine;
    HomeWatch home;
    LostBeacon beacon;
    double north = 0, east = 0;           // where the dog really is
    double anchorNorth = 0, anchorEast = 0;
    double anchorRangeM = 15;
    uint8_t sats = 16;                    // satellites in each fix
    bool relayed = true;                  // the base answers
    uint32_t now = 1000;
    float clearance = 0;                  // the newest fix's (0: none yet, which fixes often)
    uint32_t nextFixMs = 0;               // 0: at once
    uint32_t lastGoodMs = 0;
    int32_t lastLat = 0, lastLon = 0;     // the position the collar reports: good fixes only
    int corrections = 0;                  // beeps, vibrations and shocks
    std::string path;                     // every state and home change, in order

    void note(const char *what)
    {
        if (!path.empty())
            path += " > ";
        path += what;
    }
    static const char *name(CollarState s)
    {
        switch (s) {
        case CollarState::NoFence:
            return "no fence";
        case CollarState::Inside:
            return "inside";
        case CollarState::Warning:
            return "band";
        case CollarState::Outside:
            return "outside";
        case CollarState::Lost:
            return "lost";
        }
        return "?";
    }
    /// The GPS interval applyGpsPacing() sets, in seconds of fix-to-fix.
    uint32_t gpsEvery() const
    {
        if (home.home())
            return kHomeGpsSecs;
        const uint32_t s = gpsIntervalSecs(engine.settings(), engine.state(), clearance, alertWidthM(engine.fences()),
                                           beacon.active(), relayed);
        return s ? s : 1;
    }
    void second()
    {
        now += 1000;
        const CollarState before = engine.state();
        const bool wasHome = home.home(), wasBeacon = beacon.active();
        if ((int32_t)(now - nextFixMs) >= 0) {
            const Fix f{latAt(north), lonAt(east), 150, sats, now};
            const Decision d = engine.onFix(f);
            const bool used = engine.fixIsGood(f) && !d.fixRejected;
            clearance = d.clearanceM;
            if (used) {
                lastGoodMs = now;
                lastLat = f.latI;
                lastLon = f.lonI;
                beacon.onFix(engine.state() != CollarState::NoFence && d.clearanceM < 0, now);
                home.onFix(engine.state() == CollarState::Inside, now);
            } else if (!engine.fixIsGood(f) && engine.state() == CollarState::Inside && engine.clearForHome(f)) {
                home.onFix(true, now);
            }
            if (d.correction != Correction::None)
                corrections++;
            nextFixMs = d.verify ? d.verifyAtMs : now + gpsEvery() * 1000;
        }
        beacon.tick(now);
        if (now % 2000 == 0 && std::hypot(north - anchorNorth, east - anchorEast) <= anchorRangeM)
            home.onSighting(-60, now);
        home.tick(now, engine.state() == CollarState::Inside && !beacon.active());
        if (home.home() != wasHome) {
            // Leaving home wakes the GPS for a fix as soon as it has one (clearance 0: fix often).
            if (!home.home()) {
                clearance = 0;
                nextFixMs = now;
            } else {
                nextFixMs = now + kHomeGpsSecs * 1000;
            }
        }
        if (engine.state() != before)
            note(name(engine.state()));
        if (beacon.active() != wasBeacon)
            note(beacon.active() ? "beacon on" : "beacon off");
        if (home.home() != wasHome)
            note(home.home() ? "home, GPS off" : "GPS on");
    }
    void stay(uint32_t secs)
    {
        for (uint32_t i = 0; i < secs; i++)
            second();
    }
    /// Walk in a straight line at speed m/s, then stop there.
    void walkTo(double n, double e, double speed)
    {
        const double dist = std::hypot(n - north, e - east);
        const int steps = (int)std::ceil(dist / speed);
        const double n0 = north, e0 = east;
        for (int i = 1; i <= steps; i++) {
            north = n0 + (n - n0) * i / steps;
            east = e0 + (e - e0) * i / steps;
            second();
        }
    }
};

static void testZoneWalk()
{
    // A yard 200 m by 170 m (danger 3, warning 5, attention 5: 13 m of bands), a pond kept out near
    // its north side (bands 4 + 4), and the home anchor in the middle of the yard.
    FenceSet f;
    f.version = 1;
    f.zoneCount = 2;
    f.zones[0] = box(ZoneKind::KeepIn, 0, 0, 200, 170, 3, 5, 5);
    f.zones[1] = box(ZoneKind::KeepOut, 150, 60, 170, 110, 0, 4, 4);
    auto fresh = [&f]() {
        WalkingCollar c;
        c.engine.setFences(f);
        c.engine.settings().gpsFastSecs = 3; // what the base sends by default
        HomeAnchor a;
        a.kind = AnchorKind::Beacon;
        a.rssiDbm = -70;
        c.home.setThreshold(a.rssiDbm);
        c.anchorNorth = 100;
        c.anchorEast = 85;
        c.north = 100;
        c.east = 85;
        return c;
    };

    // Home, out east through every band, past the line, lost, the beacon, back in, and home again.
    {
        WalkingCollar c = fresh();
        c.stay(60);
        CHECK(c.home.home() && c.engine.state() == CollarState::Inside && c.gpsEvery() == kHomeGpsSecs);
        // Deep in the yard and away from home: slower fixes (47 m from the bands: between 35 and 100).
        c.walkTo(60, 85, 2); // 60 m from the south line, 72 m from the pond's bands
        c.stay(20);
        CHECK(!c.home.home() && c.gpsEvery() > 3 && c.gpsEvery() < kMidGpsSecs);
        // Within 35 m of the bands: the owner's 3 s.
        c.walkTo(100, 140, 2);
        c.stay(kMidGpsSecs); // the fix after the slow one
        CHECK(c.engine.state() == CollarState::Inside && c.gpsEvery() == 3);
        // Through the bands at a walk (corrections, after a second look each), out past the line.
        c.walkTo(100, 200, 1);
        CHECK(c.corrections > 0);
        c.stay(5);
        CHECK(c.engine.state() == CollarState::Lost && c.gpsEvery() == kLostGpsSecs);
        const int corrected = c.corrections;
        c.stay(LostBeacon::kTriggerMs / 1000 + 10);
        CHECK(c.beacon.active());
        // Out of the base's reach: the GPS powered down until it answers again.
        c.relayed = false;
        CHECK(c.gpsEvery() == kLostOffGpsSecs);
        c.relayed = true;
        // Back in through the bands while lost: no corrections at all, and still lost until 3
        // fixes clear of every band.
        c.walkTo(100, 160, 1);
        c.stay(kLostGpsSecs + 5);
        CHECK(c.engine.state() == CollarState::Lost && !c.beacon.active() && c.corrections == corrected);
        c.walkTo(100, 85, 2);
        c.stay(4 * kLostGpsSecs);
        CHECK(c.engine.state() == CollarState::Inside && c.corrections == corrected);
        CHECK(c.home.home());
        CHECK(c.path == "home, GPS off > GPS on > band > outside > lost > beacon on > beacon off > inside > home, GPS off");
    }

    // Into the pond (a keep-out zone) and out again, from home.
    {
        WalkingCollar c = fresh();
        c.stay(60);
        c.walkTo(160, 85, 1); // the middle of the pond
        c.stay(10);
        CHECK(c.engine.state() == CollarState::Lost);
        c.walkTo(100, 85, 1);
        c.stay(4 * kLostGpsSecs);
        CHECK(c.engine.state() == CollarState::Inside && c.home.home());
        CHECK(c.path == "home, GPS off > GPS on > band > outside > lost > inside > home, GPS off");
    }

    // Into a band and back, without crossing: corrections, then quiet and inside again.
    {
        WalkingCollar c = fresh();
        c.walkTo(100, 160, 2); // 10 m from the east line: the attention and warning bands
        c.stay(10);
        CHECK(c.engine.state() == CollarState::Warning && c.corrections > 0 && !c.home.home());
        c.walkTo(100, 85, 2);
        c.stay(30);
        CHECK(c.engine.state() == CollarState::Inside && c.home.home());
        CHECK(c.path == "band > inside > home, GPS off");
    }

    // The anchor heard from inside a band (a beacon too close to the line): never home.
    {
        WalkingCollar c = fresh();
        c.anchorNorth = 100;
        c.anchorEast = 165;
        c.walkTo(100, 165, 2);
        c.stay(120);
        CHECK(c.engine.state() == CollarState::Warning && !c.home.home());
    }

    // Too few satellites: no fix counts, so nothing is decided. A dog that walks out under trees
    // (10 satellites) is not noticed until a fix from 12 or more puts it outside; the position
    // reported stays the last good one.
    {
        WalkingCollar c = fresh();
        c.walkTo(100, 140, 2);
        c.stay(5);
        const int32_t lat = c.lastLat, lon = c.lastLon;
        c.sats = 10;
        c.walkTo(100, 200, 1);
        c.stay(30);
        CHECK(c.engine.state() == CollarState::Inside && c.corrections == 0 && !c.beacon.active());
        CHECK(c.lastLat == lat && c.lastLon == lon);
        c.sats = 12;
        c.stay(20);
        CHECK(c.engine.state() == CollarState::Lost && c.lastLon != lon);
    }

    // Near the house a collar may see 8 to 11 satellites for hours. Such a fix is no position for
    // the fences, but it is proof enough for home mode that the dog is well clear of the bands.
    {
        WalkingCollar c = fresh();
        c.sats = 10;
        c.stay(60);
        CHECK(c.home.home() && c.gpsEvery() == kHomeGpsSecs && c.lastLat == 0);
        // Out of the anchor's range and back: home again, from a 10-satellite fix.
        c.walkTo(60, 85, 2);
        c.stay(15 * 60); // longer than HomeWatch::kFreshMs: only a new fix can bring it home
        CHECK(!c.home.home());
        c.walkTo(100, 85, 2);
        c.stay(60);
        CHECK(c.home.home());
        CHECK(c.path == "home, GPS off > GPS on > home, GPS off");
    }
    // Fewer than 8 satellites: still never home.
    {
        WalkingCollar c = fresh();
        c.sats = 7;
        c.stay(120);
        CHECK(!c.home.home());
    }
    // An anchor close to the bands: a poorer fix within 10 m of them does not count, a good one does.
    {
        WalkingCollar c = fresh();
        c.anchorNorth = 100;
        c.anchorEast = 150; // 20 m from the east line: 7 m clear of its 13 m of bands
        c.east = 150;
        c.sats = 10;
        c.stay(120);
        CHECK(c.engine.state() == CollarState::Inside && !c.home.home());
        c.sats = 12;
        c.stay(60);
        CHECK(c.home.home());
    }
}

int main()
{
    testCaiXianLin();
    testGeometry();
    testStateMachine();
    testFenceReload();
    testShockCaps();
    testBands();
    testSecondLook();
    testPulser();
    testBeacon();
    testHomeWatch();
    testZoneWalk();
    testBaseLink();
    testGpsPacing();
    testFixWatch();
    testProtocol();
    testSha256();
    testAppLink();
    std::printf("%d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
