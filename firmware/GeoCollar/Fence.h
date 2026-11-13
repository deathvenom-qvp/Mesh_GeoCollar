// SPDX-License-Identifier: GPL-3.0-or-later
//
// Geofence geometry and the collar's correction state machine.
// Pure C++ with no Arduino or Meshtastic dependencies, so it is unit tested on a PC.

#pragma once

#include <cstddef>
#include <cstdint>

namespace geocollar
{

constexpr size_t kMaxZones = 4;
constexpr size_t kMaxVertices = 32;

enum class ZoneKind : uint8_t {
    KeepIn = 0,  // the dog should stay inside this polygon
    KeepOut = 1, // the dog should stay out of this polygon (pond, road)
};

/// A polygon in Meshtastic's integer degrees (degrees * 1e7).
struct Zone {
    ZoneKind kind = ZoneKind::KeepIn;
    uint16_t warnMarginM = 10; // start warning this many metres before the line
    uint8_t vertexCount = 0;
    int32_t latI[kMaxVertices] = {};
    int32_t lonI[kMaxVertices] = {};
};

struct FenceSet {
    uint16_t version = 0;
    uint8_t zoneCount = 0;
    Zone zones[kMaxZones];
};

/// Signed distance in metres from a point to the zone boundary, positive on the allowed side
/// (inside a keep-in zone, outside a keep-out zone). Uses a local flat-earth projection, which is
/// accurate to well under a metre over a few kilometres.
float signedDistanceM(const Zone &zone, int32_t latI, int32_t lonI);

/// The smallest signed distance over all zones: the tightest constraint wins. A point that breaks
/// any zone is negative. Returns a large positive value when there are no zones.
float clearanceM(const FenceSet &fences, int32_t latI, int32_t lonI);

/// The smallest warning margin configured on any zone.
uint16_t warnMarginM(const FenceSet &fences);

// ---------------------------------------------------------------------------------------------
// State machine
// ---------------------------------------------------------------------------------------------

enum class CollarState : uint8_t {
    NoFence = 0,   // no fences loaded, nothing to do
    Inside = 1,    // well inside
    Warning = 2,   // inside but within the warning margin
    Outside = 3,   // crossed the line, not yet confirmed
    Lost = 4,      // confirmed outside: corrections stop, tracking starts
};

struct Settings {
    bool correctionsEnabled = true; // master switch for beep, vibrate and shock
    bool shockEnabled = false;      // shock is off unless the owner turns it on
    uint8_t shockLevel = 10;        // 0..99, capped by maxShockLevel
    uint8_t maxShockLevel = 30;
    uint8_t vibrateLevel = 50;
    uint8_t maxShocksPerBreach = 2;
    uint8_t maxShocksPerHour = 4;
    uint8_t lostConfirmFixes = 3;   // consecutive good fixes outside before Lost
    uint8_t rearmFixes = 3;         // consecutive good fixes back inside before re-arming
    uint16_t rearmMarginM = 5;      // must be this far inside to count toward re-arming
    uint16_t maxPdopX100 = 500;     // ignore fixes worse than PDOP 5.0
    uint8_t minSats = 5;
    uint16_t correctionMs = 1000;   // how long each beep/vibrate/shock lasts
    // GPS pacing: fix often near the line, rarely deep inside, to save battery.
    bool gpsPacing = true;
    uint16_t gpsFastSecs = 5;       // at or near the line (10 s or less keeps the GPS always on)
    uint16_t gpsSlowSecs = 60;      // deep inside
    uint16_t gpsLostSecs = 15;      // while lost, for tracking
    uint8_t dogSpeedMps = 6;        // assumed top speed: next fix before the dog can reach the margin
};

struct Fix {
    int32_t latI;
    int32_t lonI;
    uint16_t pdopX100; // 0 = unknown
    uint8_t sats;      // 0 = unknown
    uint32_t nowMs;    // monotonic milliseconds
};

enum class Correction : uint8_t { None, Beep, Vibrate, Shock };

struct Decision {
    Correction correction = Correction::None;
    uint8_t intensity = 0;
    bool sendBreach = false;    // send a breach alert to the base now
    bool sendStatus = false;    // state changed: tell the base
    bool fixRejected = false;   // fix too poor to act on
    float clearanceM = 0;
};

class FenceEngine
{
  public:
    void setFences(const FenceSet &fences);
    const FenceSet &fences() const { return fences_; }
    Settings &settings() { return settings_; }
    const Settings &settings() const { return settings_; }

    CollarState state() const { return state_; }

    /// Manual override from the base: jump to Lost (e.g. the owner knows the dog is missing).
    void forceLost();
    /// Manual override from the base: leave Lost and re-arm.
    void clearLost();

    /// Feed one GPS fix; returns what the collar should do now.
    Decision onFix(const Fix &fix);

  private:
    bool fixIsGood(const Fix &fix) const;
    bool shockAllowed(uint32_t nowMs);
    void setState(CollarState s, Decision &d);

    FenceSet fences_;
    Settings settings_;
    CollarState state_ = CollarState::NoFence;
    uint8_t outsideCount_ = 0;
    uint8_t rearmCount_ = 0;
    uint8_t shocksThisBreach_ = 0;
    // Timestamps of recent shocks, for the per-hour cap.
    static constexpr size_t kShockHistory = 16;
    uint32_t shockTimes_[kShockHistory] = {};
    size_t shockHead_ = 0;
    size_t shockCount_ = 0;
};

/// GPS update interval in seconds for the current state and clearance, or 0 to leave Meshtastic's
/// own configured interval alone (no fences, or pacing turned off). Inside, the interval is the
/// time the dog needs at dogSpeedMps to cover the distance to the warning margin, clamped between
/// gpsFastSecs and gpsSlowSecs.
uint32_t gpsIntervalSecs(const Settings &s, CollarState state, float clearanceM, uint16_t warnMarginM);

} // namespace geocollar
