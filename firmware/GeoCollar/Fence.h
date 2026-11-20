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
/// The second look. Before a correction starts, or steps up to a stronger one, the collar waits
/// this long and takes another GPS reading; only if that one agrees does the correction go out.
constexpr uint32_t kVerifyMs = 1000;
/// A first reading older than this is too old to be agreed with: the wait starts again.
constexpr uint32_t kVerifyStaleMs = 30000;

/// A GPS fix counts only if at least this many satellites were used for it: fewer, and the fix is
/// ignored entirely, as if there were none (no fence decision, no position sent, the base told
/// "no fix" once the last good one is overdue). The owner's choice, fixed. Under trees or against a
/// wall a fix may have fewer for minutes at a time, and in that time the collar corrects nothing
/// and cannot notice the dog leaving.
constexpr uint8_t kMinFixSats = 12;

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
    uint8_t minSats = kMinFixSats;  // ignore fixes from fewer satellites (or an unknown number)
    uint16_t correctionMs = 1000;   // how long each beep/vibrate/shock lasts
    // GPS pacing: fix often near the line, rarely deep inside, to save battery (gpsIntervalSecs).
    // The owner's GPS interval near the bands from the base (0.19.6 on, setGpsInsideSecs) is gpsFastSecs.
    bool gpsPacing = true;
    uint16_t gpsFastSecs = 5;       // at or near the line (10 s or less keeps the GPS always on)
    uint16_t gpsSlowSecs = 60;      // unused since 0.19.6 (kMidGpsSecs, kFarGpsSecs); kept for the state file
    uint16_t gpsLostSecs = 15;      // unused since 0.19.6 (kLostGpsSecs); kept for the state file
    uint8_t dogSpeedMps = 6;        // unused since 0.19.6 (pacing steps at kNearAlertM); kept for the state file
};

/// Bring settings read back from flash into the ranges the firmware relies on (levels at most 99,
/// counts and intervals at least 1), so a damaged or foreign file cannot make it misbehave.
void sanitizeSettings(Settings &s);

/// The owner's GPS interval inside the fences: a fix every `secs` seconds while the dog is within
/// kNearAlertM of the outermost band (0 = the GPS always on, reporting every second); further in,
/// slower (see gpsIntervalSecs). Returns true if the settings changed.
bool setGpsInsideSecs(Settings &s, uint8_t secs);

/// Inside and at most this far from the outermost band, the collar fixes every gpsFastSecs.
constexpr float kNearAlertM = 35.0f;
/// From kNearAlertM to this far, the interval rises in step with the distance, to kMidGpsSecs.
constexpr float kMidAlertM = 100.0f;
constexpr uint32_t kMidGpsSecs = 120;
/// Further than kMidAlertM from the outermost band, a fix every this many seconds.
constexpr uint32_t kFarGpsSecs = 180;

/// Past a line (not yet confirmed lost) the collar fixes at least this often, whatever the
/// interval inside, so that a dog that got out is confirmed lost, or seen back inside, quickly.
constexpr uint32_t kOutsideGpsSecs = 5;

struct Fix {
    int32_t latI;
    int32_t lonI;
    uint16_t pdopX100; // 0 = unknown
    uint8_t sats;      // satellites used; 0 = unknown, which is not enough (kMinFixSats)
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
    // A correction is held back until another reading agrees with this one. A reading counts as
    // the second look from verifyAtMs on: the caller waits until then and gets one from the GPS.
    bool verify = false;
    uint32_t verifyAtMs = 0;
};

class FenceEngine
{
  public:
    /// Load new fences. A dog past a line or lost stays Outside or Lost, with no corrections
    /// until it is clear of every band of the new fences; otherwise the engine starts afresh.
    void setFences(const FenceSet &fences);
    const FenceSet &fences() const { return fences_; }
    Settings &settings() { return settings_; }
    const Settings &settings() const { return settings_; }

    CollarState state() const { return state_; }

    /// Manual override from the base: jump to Lost (e.g. the owner knows the dog is missing).
    void forceLost();
    /// Manual override from the base: leave Lost and re-arm.
    void clearLost();

    /// Whether a fix is good enough to use at all: a position, PDOP no worse than maxPdopX100,
    /// and at least minSats satellites. The caller keeps a position only if it is.
    bool fixIsGood(const Fix &fix) const;

    /// Feed one GPS fix; returns what the collar should do now.
    ///
    /// One reading alone never starts a correction. The first reading in a band (or in a more
    /// severe band than before) only starts a wait of kVerifyMs. The correction goes out when a
    /// reading taken after that wait still has the dog there, and it is the correction for the
    /// least severe band of those readings: a shock takes two readings in the danger band, at
    /// least kVerifyMs apart. While the dog stays in a band that has been agreed on, each reading
    /// corrects at once. A reading in a milder band counts at once too, so going deeper again is
    /// checked again.
    Decision onFix(const Fix &fix);

  private:
    Decision evaluate(const Fix &fix);
    bool shockAllowed(uint32_t nowMs);
    /// Shock if allowed (enabled, under the per-breach and per-hour caps), otherwise vibrate.
    void correctHard(Decision &d, uint32_t nowMs);
    void setState(CollarState s, Decision &d);
    /// Take one reading's band into the second look; afterwards agreed_ is the band to correct for.
    void secondLook(Band band, uint32_t nowMs);
    void resetSecondLook();

    FenceSet fences_;
    Settings settings_;
    CollarState state_ = CollarState::NoFence;
    uint8_t outsideCount_ = 0;
    uint8_t rearmCount_ = 0;
    uint8_t shocksThisBreach_ = 0;
    bool quiet_ = false; // went past the line: no corrections until clear of every band
    // The second look (see onFix).
    Band agreed_ = Band::Clear;      // the most severe band that two readings kVerifyMs apart agree on
    bool verifying_ = false;         // a reading has the dog in a more severe band than agreed_
    uint32_t verifyMs_ = 0;          // when the first such reading came
    Band verifyBand_ = Band::Clear;  // the least severe band of the readings since then
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
/// own configured interval alone (no fences, or pacing turned off). Inside, by the distance to the
/// outermost band (alertWidthM): within kNearAlertM, gpsFastSecs; from there to kMidAlertM, rising
/// in step with the distance to kMidGpsSecs; further in, kFarGpsSecs. These are the owner's
/// choice, for battery in big yards, over steps scaled by the dog's speed: a dog that runs from
/// just past kNearAlertM can reach the line before the next fix.
/// GPS fix interval in seconds (0 = leave the owner's setting). A lost dog (lost, or the beacon
/// on) gets a fix every kLostGpsSecs while something can pass its position on (relayed: the base
/// answers, directly or through the mesh, or the phone app is connected, see BaseLink), and the GPS
/// is powered down otherwise (kLostOffGpsSecs). Inside the fences
/// the pace never depends on the base: the collar enforces the fence on its own. sameEverywhere:
/// the owner chose gpsFastSecs wherever the dog is inside, whatever the distance to the bands (it
/// changes nothing once the dog is lost or past a line).
uint32_t gpsIntervalSecs(const Settings &s, CollarState state, float clearanceM, uint16_t alertWidthM, bool beacon = false,
                         bool relayed = true, bool sameEverywhere = false);

/// How long without a fix before the last one counts as old: the GPS interval (0 = Meshtastic's
/// default, 2 minutes) plus a minute for the GPS to wake and search. At 10 s or less the GPS
/// stays on and reports all the time, and 45 s without a fix means the lock is gone.
uint32_t fixOverdueMs(uint32_t gpsIntervalSecs);

/// The GPS update interval to give Meshtastic so that its fixes come every fixEverySecs. For an
/// interval over 10 s Meshtastic 2.8 lets the GPS sleep between fixes, and after each lock holds
/// it on for min(interval - 10 s, 20 s) before it reports the fix and starts counting the interval
/// again; so a fix comes every interval plus that hold. 10 s or less keeps the GPS on, reporting
/// every second, and is returned unchanged.
uint32_t gpsSettingSecs(uint32_t fixEverySecs);

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

// ---------------------------------------------------------------------------------------------
// Home mode: the GPS off while the collar is close to a Bluetooth anchor deep inside the fences
// ---------------------------------------------------------------------------------------------

enum class AnchorKind : uint8_t {
    None = 0,
    Beacon = 1,    // a Bluetooth beacon, known by its address
    BaseRadio = 2, // the base station's Meshtastic radio, known by its Bluetooth name ("..._1a2b")
};

/// The anchor the base chose. A reading of its signal at least rssiDbm strong counts as near.
struct HomeAnchor {
    AnchorKind kind = AnchorKind::None;
    uint8_t address[6] = {}; // Beacon: its address as written, most significant byte first
    int8_t rssiDbm = -70;
};
constexpr int8_t kMinAnchorRssi = -100, kMaxAnchorRssi = -30;

/// Decides when the collar is home. Home means: the anchor has been heard at least as strong as
/// its threshold, in every reading, for kEnterMs. No GPS fix is needed (the owner's choice,
/// 0.19.14): a collar that hears the anchor is at the house, often indoors, where a fix may take
/// minutes or never come. Home ends as soon as no reading in kLeaveMs is within kStayMarginDb of
/// the threshold, or when home is no longer allowed (fences changed, past a line or lost, the
/// lost beacon on, the anchor turned off).
class HomeWatch
{
  public:
    static constexpr uint32_t kEnterMs = 20 * 1000;
    static constexpr uint32_t kLeaveMs = 7 * 1000;
    static constexpr int kStayMarginDb = 6;

    void setThreshold(int8_t rssiDbm) { threshold_ = rssiDbm; }
    /// The anchor or the fences changed: away, and every reading so far forgotten.
    void reset();
    /// A reading of the anchor's signal.
    void onSighting(int rssiDbm, uint32_t nowMs);
    /// Call about once a second. allowed: an anchor is set, fences are loaded, the dog is not past
    /// a line or lost, and the lost beacon is off. Returns true when home() changed (here or in reset).
    bool tick(uint32_t nowMs, bool allowed);
    bool home() const { return home_; }

  private:
    void leave();
    int threshold_ = -70;
    bool home_ = false;
    bool changed_ = false;
    bool strong_ = false;  // an unbroken run of strong readings is going on
    uint32_t strongSinceMs_ = 0;
    uint32_t lastStrongMs_ = 0;
    uint32_t lastStayMs_ = 0; // home: the last reading strong enough to stay
};

/// Whether the collar is in range of the anchor, for the owner to see (0.19.16): unlike HomeWatch it
/// does not wait, and holds whatever the GPS, the fences or the dog's state. In range from the
/// first reading at the threshold or stronger, until no reading in kLeaveMs is within
/// kStayMarginDb of it. kLeaveMs spans a few of the scans away from home (every 5 s), so one
/// missed scan does not end it.
class AnchorRange
{
  public:
    static constexpr uint32_t kLeaveMs = 15 * 1000;
    static constexpr int kStayMarginDb = HomeWatch::kStayMarginDb;

    void setThreshold(int8_t rssiDbm) { threshold_ = rssiDbm; }
    int8_t threshold() const { return threshold_; }
    /// The anchor changed or was turned off: out of range, readings forgotten.
    void reset();
    /// A reading of the anchor's signal.
    void onSighting(int rssiDbm, uint32_t nowMs);
    /// Call about once a second. Returns true when inRange() changed (here or in reset).
    bool tick(uint32_t nowMs);
    bool inRange() const { return inRange_; }
    /// The latest reading, if it is no older than kLeaveMs; INT8_MIN (-128) otherwise.
    int8_t latest(uint32_t nowMs) const;

  private:
    int8_t threshold_ = -70;
    bool inRange_ = false;
    bool changed_ = false;
    bool heard_ = false;
    int8_t lastRssi_ = -128;
    uint32_t lastHeardMs_ = 0;
    uint32_t lastStayMs_ = 0; // the last reading strong enough to stay in range
};

/// The GPS update interval while home: Meshtastic powers the GPS down and leaves it there.
constexpr uint32_t kHomeGpsSecs = 3600;

// ---------------------------------------------------------------------------------------------
// Out of reach: a lost dog's GPS slowed while nothing can pass its position on
// ---------------------------------------------------------------------------------------------

/// Whether the base station is answering, directly or through other mesh nodes. Every report to
/// the base asks for an acknowledgement, and the base counts as heard whenever anything from it
/// arrives: that acknowledgement, a message for the collar, or one of its broadcasts. The base is
/// out of reach once kMissLimit reports in a row have gone unanswered and nothing has come from it
/// for kSilentMs since the first of them; it is back in reach the moment anything from it
/// arrives. In reach until shown otherwise, so a collar that has just started, or has no base yet,
/// paces its GPS as usual. A forged packet "from the base" can only keep it in reach, which keeps
/// the GPS at its usual pace: the safe side.
class BaseLink
{
  public:
    static constexpr uint8_t kMissLimit = 3;
    static constexpr uint32_t kSilentMs = 2 * 60 * 1000;

    /// A report that asks for an acknowledgement went to the base.
    void onSent(uint32_t nowMs);
    /// Something came from the base.
    void onHeard();
    /// Call about once a second. Returns true when reachable() changed (here or in onHeard).
    bool tick(uint32_t nowMs);
    bool reachable() const { return reachable_; }

  private:
    bool reachable_ = true;
    bool changed_ = false;
    uint8_t unanswered_ = 0;         // reports sent since the base was last heard
    uint32_t firstUnansweredMs_ = 0; // when the first of them went
};

/// While the dog is lost and the base is out of reach, the collar reports this often instead,
/// every 5 minutes, at the same moment as the plea on the public channel. Each report asks for an
/// acknowledgement, so it is also the check for the base: an answer brings it back in reach, which
/// wakes the GPS, and the first good fix goes to the base at once.
constexpr uint32_t kLostProbeMs = LostBeacon::kMeshEveryMs;

/// How often to report to the base, in ms. normalMs: the owner's report interval. While lost, at
/// least every lostMs (the base's map follows the dog), or every kLostProbeMs while the base is
/// out of reach (a report it cannot hear costs a transmission and its retries, for nothing).
uint32_t reportIntervalMs(uint32_t normalMs, uint32_t lostMs, bool lost, bool baseReachable);

/// The GPS fix interval for a lost dog while its position can be passed on (the owner's choice).
constexpr uint32_t kLostGpsSecs = 60;
/// While nothing can pass it on: the GPS powered down, as in home mode. Meshtastic still looks
/// about once an hour, which also notices a dog that came home while the base was down.
constexpr uint32_t kLostOffGpsSecs = 3600;

} // namespace geocollar
