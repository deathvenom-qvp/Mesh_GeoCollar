// SPDX-License-Identifier: GPL-3.0-or-later

#include "Fence.h"

#include <cstdio>
#include <cstring>

#include <cmath>

namespace geocollar
{

namespace
{
constexpr double kEarthRadiusM = 6371008.8;
constexpr double kDegToRad = 3.14159265358979323846 / 180.0;
constexpr double kIntToDeg = 1e-7;

struct Vec {
    double x;
    double y;
};

/// Equirectangular projection around a reference point, in metres.
Vec project(int32_t latI, int32_t lonI, int32_t refLatI, int32_t refLonI, double cosRef)
{
    const double dLat = (static_cast<double>(latI) - refLatI) * kIntToDeg * kDegToRad;
    const double dLon = (static_cast<double>(lonI) - refLonI) * kIntToDeg * kDegToRad;
    return {dLon * cosRef * kEarthRadiusM, dLat * kEarthRadiusM};
}

double distToSegment(Vec p, Vec a, Vec b)
{
    const double vx = b.x - a.x, vy = b.y - a.y;
    const double wx = p.x - a.x, wy = p.y - a.y;
    const double len2 = vx * vx + vy * vy;
    double t = len2 > 0 ? (wx * vx + wy * vy) / len2 : 0;
    if (t < 0)
        t = 0;
    else if (t > 1)
        t = 1;
    const double dx = p.x - (a.x + t * vx), dy = p.y - (a.y + t * vy);
    return std::sqrt(dx * dx + dy * dy);
}
} // namespace

float signedDistanceM(const Zone &zone, int32_t latI, int32_t lonI)
{
    const size_t n = zone.vertexCount;
    if (n < 3)
        return 1e9f; // a degenerate zone constrains nothing

    const int32_t refLat = zone.latI[0], refLon = zone.lonI[0];
    const double cosRef = std::cos(refLat * kIntToDeg * kDegToRad);
    const Vec p = project(latI, lonI, refLat, refLon, cosRef);

    bool inside = false;
    double best = 1e18;
    Vec prev = project(zone.latI[n - 1], zone.lonI[n - 1], refLat, refLon, cosRef);
    for (size_t i = 0; i < n; i++) {
        const Vec cur = project(zone.latI[i], zone.lonI[i], refLat, refLon, cosRef);
        // Ray casting for inside/outside.
        if ((cur.y > p.y) != (prev.y > p.y)) {
            const double xCross = prev.x + (p.y - prev.y) * (cur.x - prev.x) / (cur.y - prev.y);
            if (p.x < xCross)
                inside = !inside;
        }
        const double d = distToSegment(p, prev, cur);
        if (d < best)
            best = d;
        prev = cur;
    }

    const bool allowed = (zone.kind == ZoneKind::KeepIn) ? inside : !inside;
    return static_cast<float>(allowed ? best : -best);
}

float clearanceM(const FenceSet &fences, int32_t latI, int32_t lonI)
{
    float best = 1e9f;
    for (size_t i = 0; i < fences.zoneCount && i < kMaxZones; i++) {
        const float d = signedDistanceM(fences.zones[i], latI, lonI);
        if (d < best)
            best = d;
    }
    return best;
}

uint16_t alertWidthM(const FenceSet &fences)
{
    uint16_t m = 0;
    for (size_t i = 0; i < fences.zoneCount && i < kMaxZones; i++) {
        const uint16_t w = bandWidthM(fences.zones[i]);
        if (w > m)
            m = w;
    }
    return m;
}

Band bandFor(const Zone &z, float d)
{
    if (d < 0)
        return Band::Outside;
    if (d < z.dangerM)
        return Band::Danger;
    if (d < (float)z.dangerM + z.warningM)
        return Band::Warning;
    if (d < (float)bandWidthM(z))
        return Band::Attention;
    return Band::Clear;
}

// ---------------------------------------------------------------------------------------------

void FenceEngine::setFences(const FenceSet &fences)
{
    fences_ = fences;
    outsideCount_ = 0;
    rearmCount_ = 0;
    shocksThisBreach_ = 0;
    quiet_ = false;
    state_ = fences_.zoneCount ? CollarState::Inside : CollarState::NoFence;
}

void FenceEngine::forceLost()
{
    state_ = CollarState::Lost;
    rearmCount_ = 0;
}

void FenceEngine::clearLost()
{
    if (state_ == CollarState::Lost)
        state_ = fences_.zoneCount ? CollarState::Inside : CollarState::NoFence;
    outsideCount_ = 0;
    rearmCount_ = 0;
    shocksThisBreach_ = 0;
    quiet_ = false;
}

bool FenceEngine::fixIsGood(const Fix &fix) const
{
    if (fix.latI == 0 && fix.lonI == 0)
        return false;
    if (fix.pdopX100 != 0 && fix.pdopX100 > settings_.maxPdopX100)
        return false;
    if (fix.sats != 0 && fix.sats < settings_.minSats)
        return false;
    return true;
}

bool FenceEngine::shockAllowed(uint32_t nowMs)
{
    if (!settings_.shockEnabled || shocksThisBreach_ >= settings_.maxShocksPerBreach)
        return false;
    size_t inLastHour = 0;
    for (size_t i = 0; i < shockCount_; i++) {
        if (nowMs - shockTimes_[i] < 3600000u)
            inLastHour++;
    }
    return inLastHour < settings_.maxShocksPerHour;
}

void FenceEngine::setState(CollarState s, Decision &d)
{
    if (s != state_) {
        state_ = s;
        d.sendStatus = true;
    }
}

Decision FenceEngine::onFix(const Fix &fix)
{
    Decision d;
    if (state_ == CollarState::NoFence)
        return d;
    if (!fixIsGood(fix)) {
        d.fixRejected = true;
        return d;
    }

    // Every zone has its own bands; the most severe band over all zones decides.
    float c = 1e9f;
    Band band = Band::Clear;
    bool shockBand = false; // in the danger band of a zone that has one
    for (size_t i = 0; i < fences_.zoneCount && i < kMaxZones; i++) {
        const Zone &z = fences_.zones[i];
        const float zd = signedDistanceM(z, fix.latI, fix.lonI);
        if (zd < c)
            c = zd;
        const Band b = bandFor(z, zd);
        if (b > band)
            band = b;
        if (b == Band::Danger && z.dangerM > 0)
            shockBand = true;
    }
    d.clearanceM = c;

    // Lost: never correct; just watch for the dog coming all the way back in.
    if (state_ == CollarState::Lost) {
        if (band == Band::Clear) {
            if (++rearmCount_ >= settings_.rearmFixes) {
                rearmCount_ = 0;
                outsideCount_ = 0;
                shocksThisBreach_ = 0;
                quiet_ = false;
                setState(CollarState::Inside, d);
            }
        } else {
            rearmCount_ = 0;
        }
        return d;
    }

    if (band == Band::Outside) {
        // Past the line: stop correcting at once. Correcting a dog that is out (or on its way
        // back) teaches the wrong thing. Stay quiet until it is clear of every band again.
        quiet_ = true;
        outsideCount_++;
        if (outsideCount_ >= settings_.lostConfirmFixes) {
            setState(CollarState::Lost, d);
            d.sendBreach = true;
            return d;
        }
        setState(CollarState::Outside, d);
        return d;
    }

    outsideCount_ = 0;
    if (band == Band::Clear) {
        shocksThisBreach_ = 0;
        quiet_ = false;
        setState(CollarState::Inside, d);
        return d;
    }

    // Inside, in a band: beep, then vibrate, then (danger band) shock. Silent while coming back
    // in after being past the line.
    setState(CollarState::Warning, d);
    if (quiet_ || !settings_.correctionsEnabled)
        return d;
    if (band == Band::Attention) {
        d.correction = Correction::Beep;
    } else if (band == Band::Danger && shockBand) {
        correctHard(d, fix.nowMs);
    } else {
        d.correction = Correction::Vibrate;
        d.intensity = settings_.vibrateLevel;
    }
    return d;
}

void FenceEngine::correctHard(Decision &d, uint32_t nowMs)
{
    if (shockAllowed(nowMs)) {
        d.correction = Correction::Shock;
        d.intensity = settings_.shockLevel < settings_.maxShockLevel ? settings_.shockLevel : settings_.maxShockLevel;
        shocksThisBreach_++;
        shockTimes_[shockHead_] = nowMs;
        shockHead_ = (shockHead_ + 1) % kShockHistory;
        if (shockCount_ < kShockHistory)
            shockCount_++;
    } else {
        d.correction = Correction::Vibrate;
        d.intensity = settings_.vibrateLevel;
    }
}

uint32_t gpsIntervalSecs(const Settings &s, CollarState state, float clearanceM, uint16_t alertWidthM, bool beacon)
{
    if (!s.gpsPacing || state == CollarState::NoFence)
        return 0;
    const uint32_t fast = s.gpsFastSecs ? s.gpsFastSecs : 1;
    if (beacon)
        return 2 * (s.gpsLostSecs ? s.gpsLostSecs : fast);
    const uint32_t slow = s.gpsSlowSecs > fast ? s.gpsSlowSecs : fast;
    switch (state) {
    case CollarState::Lost:
        return s.gpsLostSecs ? s.gpsLostSecs : fast;
    case CollarState::Warning:
    case CollarState::Outside:
        return fast;
    default:
        break;
    }
    const float room = clearanceM - (float)alertWidthM;
    if (!(room > 0) || s.dogSpeedMps == 0)
        return fast;
    const float secs = room / (float)s.dogSpeedMps;
    if (secs <= (float)fast)
        return fast;
    if (secs >= (float)slow)
        return slow;
    return (uint32_t)secs;
}

// ---------------------------------------------------------------------------------------------

bool CorrectionPulser::onDecision(const Decision &d, const FenceSet &f, uint32_t nowMs)
{
    fixMs_ = nowMs;
    const bool repeatable = d.correction == Correction::Beep || d.correction == Correction::Vibrate;
    if (!repeatable) {
        // Shock is never repeated between fixes, and None ends any pulsing.
        current_ = Correction::None;
        return d.correction != Correction::None;
    }
    const uint8_t period = periodFor(d.correction, f);
    const bool changed = d.correction != current_;
    current_ = d.correction;
    intensity_ = d.intensity;
    if (period == 0 || changed || nowMs - lastMs_ >= period * 1000u) {
        lastMs_ = nowMs;
        return true;
    }
    return false; // a pulse went out recently; the next one is on the pulse timer
}

Correction CorrectionPulser::poll(const FenceSet &f, uint32_t nowMs, bool allowed, uint8_t &intensity)
{
    if (current_ == Correction::None)
        return Correction::None;
    if (!allowed || nowMs - fixMs_ > kStaleMs) {
        current_ = Correction::None; // corrections switched off, or no fresh fix: stop pulsing
        return Correction::None;
    }
    const uint8_t period = periodFor(current_, f);
    if (period == 0 || nowMs - lastMs_ < period * 1000u)
        return Correction::None;
    lastMs_ = nowMs;
    intensity = intensity_;
    return current_;
}

uint8_t CorrectionPulser::periodFor(Correction c, const FenceSet &f)
{
    return c == Correction::Beep ? f.beepPulseS : c == Correction::Vibrate ? f.vibratePulseS : 0;
}

// ---------------------------------------------------------------------------------------------

void LostBeacon::onFix(bool outside, uint32_t nowMs)
{
    if (!outside) {
        // Back inside: everything stops, and a silenced beacon is armed again for next time.
        outside_ = active_ = silenced_ = false;
        return;
    }
    if (!outside_) {
        outside_ = true;
        outsideSinceMs_ = nowMs;
    }
    tick(nowMs);
}

void LostBeacon::tick(uint32_t nowMs)
{
    if (outside_ && !active_ && !silenced_ && nowMs - outsideSinceMs_ >= kTriggerMs) {
        active_ = true;
        activeSinceMs_ = nowMs;
        meshSent_ = false;
    }
}

void LostBeacon::silence()
{
    if (outside_)
        silenced_ = true;
    active_ = false;
}

void LostBeacon::reset()
{
    outside_ = active_ = silenced_ = meshSent_ = false;
}

bool LostBeacon::bleOn(uint32_t nowMs) const
{
    return active_ && (nowMs - activeSinceMs_) % (kBleOnMs + kBleOffMs) < kBleOnMs;
}

bool LostBeacon::meshDue(uint32_t nowMs)
{
    if (!active_ || (meshSent_ && nowMs - lastMeshMs_ < kMeshEveryMs))
        return false;
    meshSent_ = true;
    lastMeshMs_ = nowMs;
    return true;
}

namespace
{
/// Phone number without spaces, dots or brackets, to save room in a Bluetooth name.
void compactPhone(const char *in, char *out, size_t cap)
{
    size_t n = 0;
    for (; *in && n + 1 < cap; in++)
        if (*in != ' ' && *in != '.' && *in != '(' && *in != ')')
            out[n++] = *in;
    out[n] = '\0';
}

/// Cut a UTF-8 string to at most max bytes without splitting a character.
size_t utf8Cut(const char *s, size_t max)
{
    size_t n = strlen(s);
    if (n <= max)
        return n;
    n = max;
    while (n > 0 && (static_cast<uint8_t>(s[n]) & 0xC0) == 0x80)
        n--;
    return n;
}
} // namespace

size_t beaconName(const char *dog, const char *phone, char *out, size_t cap)
{
    if (!cap)
        return 0;
    char ph[32];
    compactPhone(phone ? phone : "", ph, sizeof(ph));
    const char *d = (dog && *dog) ? dog : "Dog";
    const size_t max = cap - 1;
    const size_t phLen = strlen(ph);
    const char *mid = phLen ? " is lost " : " is lost";
    // Full form, then without "is", then with the dog's name cut short.
    size_t need = strlen(d) + strlen(mid) + phLen;
    if (need > max && phLen)
        mid = " lost ";
    const size_t fixed = strlen(mid) + phLen;
    size_t dogLen = fixed < max ? utf8Cut(d, max - fixed) : 0;
    int n = snprintf(out, cap, "%.*s%s%s", (int)dogLen, d, mid, ph);
    if (n < 0)
        return 0;
    return (size_t)n < cap ? (size_t)n : cap - 1;
}

size_t beaconText(const char *dog, const char *owner, const char *phone, int32_t latI, int32_t lonI, char *out, size_t cap)
{
    const char *d = (dog && *dog) ? dog : "this dog";
    int n;
    if (phone && *phone)
        n = snprintf(out, cap, "Please help get %s home. Call %s%s%s.", d, owner && *owner ? owner : "", owner && *owner ? " " : "", phone);
    else
        n = snprintf(out, cap, "Please help get %s home.", d);
    if (n > 0 && (size_t)n < cap && (latI || lonI)) {
        const int32_t la = latI < 0 ? -latI : latI, lo = lonI < 0 ? -lonI : lonI;
        n += snprintf(out + n, cap - n, " Last seen %s%d.%05d,%s%d.%05d", latI < 0 ? "-" : "", (int)(la / 10000000),
                      (int)((la % 10000000) / 100), lonI < 0 ? "-" : "", (int)(lo / 10000000), (int)((lo % 10000000) / 100));
    }
    if (n < 0)
        return 0;
    return (size_t)n < cap ? (size_t)n : cap - 1;
}

} // namespace geocollar
