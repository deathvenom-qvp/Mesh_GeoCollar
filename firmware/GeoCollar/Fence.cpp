// SPDX-License-Identifier: GPL-3.0-or-later

#include "Fence.h"

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

uint16_t warnMarginM(const FenceSet &fences)
{
    uint16_t m = 0;
    for (size_t i = 0; i < fences.zoneCount && i < kMaxZones; i++) {
        if (i == 0 || fences.zones[i].warnMarginM < m)
            m = fences.zones[i].warnMarginM;
    }
    return m;
}

// ---------------------------------------------------------------------------------------------

void FenceEngine::setFences(const FenceSet &fences)
{
    fences_ = fences;
    outsideCount_ = 0;
    rearmCount_ = 0;
    shocksThisBreach_ = 0;
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

    const float c = clearanceM(fences_, fix.latI, fix.lonI);
    d.clearanceM = c;

    // Lost: never correct; just watch for the dog coming back well inside.
    if (state_ == CollarState::Lost) {
        if (c >= settings_.rearmMarginM) {
            if (++rearmCount_ >= settings_.rearmFixes) {
                rearmCount_ = 0;
                outsideCount_ = 0;
                shocksThisBreach_ = 0;
                setState(CollarState::Inside, d);
            }
        } else {
            rearmCount_ = 0;
        }
        return d;
    }

    const float margin = warnMarginM(fences_);

    if (c >= margin) {
        outsideCount_ = 0;
        shocksThisBreach_ = 0;
        setState(CollarState::Inside, d);
        return d;
    }

    if (c >= 0) {
        // Inside, but close to the line: warn, harder the closer it gets.
        outsideCount_ = 0;
        setState(CollarState::Warning, d);
        if (settings_.correctionsEnabled) {
            if (c < margin / 2) {
                d.correction = Correction::Vibrate;
                d.intensity = settings_.vibrateLevel;
            } else {
                d.correction = Correction::Beep;
            }
        }
        return d;
    }

    // Outside the line.
    outsideCount_++;
    if (outsideCount_ >= settings_.lostConfirmFixes) {
        setState(CollarState::Lost, d);
        d.sendBreach = true;
        return d; // no correction once lost
    }

    setState(CollarState::Outside, d);
    if (!settings_.correctionsEnabled)
        return d;

    if (shockAllowed(fix.nowMs)) {
        d.correction = Correction::Shock;
        d.intensity = settings_.shockLevel < settings_.maxShockLevel ? settings_.shockLevel : settings_.maxShockLevel;
        shocksThisBreach_++;
        shockTimes_[shockHead_] = fix.nowMs;
        shockHead_ = (shockHead_ + 1) % kShockHistory;
        if (shockCount_ < kShockHistory)
            shockCount_++;
    } else {
        d.correction = Correction::Vibrate;
        d.intensity = settings_.vibrateLevel;
    }
    return d;
}

} // namespace geocollar
