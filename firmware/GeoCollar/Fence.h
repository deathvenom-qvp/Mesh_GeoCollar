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
/// How many recent shocks the hourly limit remembers; Settings::maxShocksPerHour cannot exceed it.
constexpr size_t kShockHistory = 16;

enum class ZoneKind : uint8_t {
    KeepIn = 0,  // the dog should stay inside this polygon
    KeepOut = 1, // the dog should stay out of this polygon (pond, road)
};

/// A polygon in Meshtastic's integer degrees (degrees * 1e7).
///
/// The polygon is the outer edge of the correction bands. Measured from the line into the allowed
/// side (inwards for keep-in, outwards for keep-out), the bands are:
///   danger    [0, dangerM)                         shock (if enabled on the collar), else vibrate
///   warning   [dangerM, dangerM + warningM)        vibrate
///   attention [.., dangerM + warningM + attentionM) beep
/// dangerM = 0 turns the danger band off, so the line itself starts the warning band and the
/// zone never asks for a shock.
struct Zone {
    ZoneKind kind = ZoneKind::KeepIn;
    uint8_t dangerM = 0;
    uint8_t warningM = 5;
    uint8_t attentionM = 5;
    uint8_t vertexCount = 0;
    int32_t latI[kMaxVertices] = {};
    int32_t lonI[kMaxVertices] = {};
};

struct FenceSet {
    uint16_t version = 0;
    uint8_t zoneCount = 0;
    // Repeat beep / vibrate every this many seconds while the dog stays in that band.
    // 0 = once per GPS fix only.
    uint8_t beepPulseS = 0;
    uint8_t vibratePulseS = 0;
    Zone zones[kMaxZones];
};

/// True when two fence sets describe the same zones, bands and pulse intervals (the version
/// number is not compared).
bool sameFences(const FenceSet &a, const FenceSet &b);

/// Signed distance in metres from a point to the zone boundary, positive on the allowed side
/// (inside a keep-in zone, outside a keep-out zone). Uses a local flat-earth projection, which is
/// accurate to well under a metre over a few kilometres.
float signedDistanceM(const Zone &zone, int32_t latI, int32_t lonI);

/// The smallest signed distance over all zones: the tightest constraint wins. A point that breaks
/// any zone is negative. Returns a large positive value when there are no zones.
float clearanceM(const FenceSet &fences, int32_t latI, int32_t lonI);

/// Total band width of a zone (danger + warning + attention).
inline uint16_t bandWidthM(const Zone &z) { return (uint16_t)z.dangerM + z.warningM + z.attentionM; }

/// The widest band width of any zone: how far inside the dog must be for every zone to be quiet.
uint16_t alertWidthM(const FenceSet &fences);

/// Which band a point is in for one zone, from quiet to outside.
enum class Band : uint8_t { Clear = 0, Attention = 1, Warning = 2, Danger = 3, Outside = 4 };
Band bandFor(const Zone &zone, float signedDistanceM);

// ---------------------------------------------------------------------------------------------
// State machine
// ---------------------------------------------------------------------------------------------

enum class CollarState : uint8_t {
    NoFence = 0,   // no fences loaded, nothing to do
    Inside = 1,    // well inside
    Warning = 2,   // inside but in a zone's attention, warning or danger band
    Outside = 3,   // crossed the line, not yet confirmed; no corrections
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
    uint16_t rearmMarginM = 5;      // unused since bands: re-arming needs the dog clear of every band
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

/// Bring settings read back from flash into the ranges the firmware relies on (levels at most 99,
/// counts and intervals at least 1), so a damaged or foreign file cannot make it misbehave.
void sanitizeSettings(Settings &s);

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
    /// Shock if allowed (enabled, under the per-breach and per-hour caps), otherwise vibrate.
    void correctHard(Decision &d, uint32_t nowMs);
    void setState(CollarState s, Decision &d);

    FenceSet fences_;
    Settings settings_;
    CollarState state_ = CollarState::NoFence;
    uint8_t outsideCount_ = 0;
    uint8_t rearmCount_ = 0;
    uint8_t shocksThisBreach_ = 0;
    bool quiet_ = false; // went past the line: no corrections until clear of every band
    // Timestamps of recent shocks, for the per-hour cap.
    uint32_t shockTimes_[kShockHistory] = {};
    size_t shockHead_ = 0;
    size_t shockCount_ = 0;
};

/// Repeats beep and vibrate corrections between GPS fixes, at the fence set's pulse intervals,
/// for as long as the latest fix keeps the dog in that band.
class CorrectionPulser
{
  public:
    static constexpr uint32_t kStaleMs = 30000; // stop pulsing if fixes stop coming

    /// Feed each fix's decision. Returns true if the decision's correction should fire now
    /// (false when a pulse of the same kind went out less than one period ago).
    bool onDecision(const Decision &d, const FenceSet &f, uint32_t nowMs);
    /// Call often. Returns a correction to repeat now (and its intensity), or None.
    Correction poll(const FenceSet &f, uint32_t nowMs, bool allowed, uint8_t &intensity);
    void stop() { current_ = Correction::None; }

  private:
    static uint8_t periodFor(Correction c, const FenceSet &f);
    Correction current_ = Correction::None;
    uint8_t intensity_ = 0;
    uint32_t lastMs_ = 0;
    uint32_t fixMs_ = 0;
};

/// GPS update interval in seconds for the current state and clearance, or 0 to leave Meshtastic's
/// own configured interval alone (no fences, or pacing turned off). Inside, the interval is the
/// time the dog needs at dogSpeedMps to cover the distance to the outermost band (alertWidthM),
/// clamped between gpsFastSecs and gpsSlowSecs.
/// GPS fix interval in seconds (0 = leave the owner's setting). With the lost beacon on, fixes
/// come half as often as in lost mode, to make the battery last.
uint32_t gpsIntervalSecs(const Settings &s, CollarState state, float clearanceM, uint16_t alertWidthM, bool beacon = false);

/// How long without a fix before the last one counts as old: the GPS interval (0 = Meshtastic's
/// default, 2 minutes) plus a minute for the GPS to wake and search. At 10 s or less the GPS
/// stays on and reports all the time, and 45 s without a fix means the lock is gone.
uint32_t fixOverdueMs(uint32_t gpsIntervalSecs);

/// Says whether the collar has a current GPS fix. There is none at boot, and none again once a
/// fix is overdue; only the next fix brings it back. A change of GPS interval gives the GPS a
/// full allowance from that moment, so going from a slow interval to a fast one does not make
/// the last fix look old before the GPS has had time to answer.
class FixWatch
{
  public:
    void onFix(uint32_t nowMs)
    {
        fixMs_ = nowMs;
        noFix_ = false;
    }
    /// Call about once a second with the GPS interval in force. Returns noFix().
    bool tick(uint32_t nowMs, uint32_t gpsIntervalSecs);
    bool noFix() const { return noFix_; }

  private:
    bool noFix_ = true;
    bool started_ = false;
    bool paceFresh_ = false; // the interval changed less than one allowance ago
    uint32_t intervalSecs_ = 0;
    uint32_t fixMs_ = 0;
    uint32_t paceMs_ = 0;
};

/// Lost-dog beacon. Once the dog has been past a line for 90 s, the collar takes a fix half as
/// often, advertises "<dog> is lost <phone>" over Bluetooth for 90 s, goes radio-quiet for 90 s,
/// and repeats; and every 5 minutes it broadcasts a plain text message on the mesh asking whoever
/// reads it to call the owner. It stops when a fix puts the dog back inside, or when the owner
/// silences it (then it stays off until the dog has been back inside once).
class LostBeacon
{
  public:
    static constexpr uint32_t kTriggerMs = 90 * 1000;
    static constexpr uint32_t kBleOnMs = 90 * 1000;
    static constexpr uint32_t kBleOffMs = 90 * 1000;
    static constexpr uint32_t kMeshEveryMs = 5 * 60 * 1000;

    /// Call with every accepted fix: outside = past a line (or with no fences, false).
    void onFix(bool outside, uint32_t nowMs);
    /// Call often (every second or so); starts the beacon once the dog has been out long enough,
    /// even if fixes stop coming.
    void tick(uint32_t nowMs);
    void silence();
    void reset();

    bool active() const { return active_; }
    bool silenced() const { return silenced_; }
    /// In the Bluetooth "on" half of the cycle.
    bool bleOn(uint32_t nowMs) const;
    /// True once when a mesh message is due (straight away, then every 5 minutes).
    bool meshDue(uint32_t nowMs);

  private:
    bool outside_ = false;
    bool active_ = false;
    bool silenced_ = false;
    bool meshSent_ = false;
    uint32_t outsideSinceMs_ = 0;
    uint32_t activeSinceMs_ = 0;
    uint32_t lastMeshMs_ = 0;
};

/// Bluetooth device name for the beacon, at most cap - 1 bytes: "Rex is lost 5551234567",
/// shortened to "Rex lost 5551234567" and then a shorter dog's name if it does not fit.
size_t beaconName(const char *dog, const char *phone, char *out, size_t cap);
/// The mesh message: "Please help get Rex home. Call Ken 5551234567." plus the last position.
size_t beaconText(const char *dog, const char *owner, const char *phone, int32_t latI, int32_t lonI, char *out, size_t cap);

} // namespace geocollar
