// SPDX-License-Identifier: GPL-3.0-or-later
//
// Meshtastic glue for GeoCollar: reads the node's own GPS position, runs the fence engine,
// drives the training collar, and talks to the base station over the mesh.
//
// Built on Meshtastic (https://meshtastic.org, GPL-3.0). CaiXianLin protocol documented by
// OpenShock (https://openshock.org). See CREDITS.md at the repository root.

#include "configuration.h"

#if defined(ARCH_ESP32)

#include "GeoCollar.h"
#include "GeoCollarConfig.h"

#include "CaiXianLin.h"
#include "Fence.h"
#include "Protocol.h"

#include "Channels.h"
#include "FSCommon.h"
#include "GPSStatus.h"
#include "MeshService.h"
#include "NodeDB.h"
#include "PowerStatus.h"
#include "SPILock.h"
#include "SafeFile.h"
#include "SinglePortModule.h"
#include "concurrency/LockGuard.h"
#include "concurrency/OSThread.h"
#include "mesh/AdminKeys.h"

#if !MESHTASTIC_EXCLUDE_GPS
#include "GPS.h"
#define GEOCOLLAR_HAS_GPS 1
#else
#define GEOCOLLAR_HAS_GPS 0
#endif

#include <Arduino.h>
#include <cstring>
#include <esp32-hal-rmt.h>

#if !defined(CONFIG_IDF_TARGET_ESP32S2) && !MESHTASTIC_EXCLUDE_BLUETOOTH
#include "main.h"
#include "nimble/NimbleBluetooth.h"
#include <BLEAdvertising.h>
#include <BLEDevice.h>
#define GEOCOLLAR_HAS_BLE 1
#else
#define GEOCOLLAR_HAS_BLE 0
#endif

using namespace geocollar;

namespace
{

constexpr const char *kStateFile = "/prefs/geocollar.bin";
constexpr uint32_t kStateMagic = 0x47434F4C; // "GCOL"
constexpr uint16_t kStateFormat = 8; // 2: GPS pacing; 3: per-zone bands; 4: pulse intervals; 5: owner; 6: command seq;
                                     // 7: report interval; 8: owner and config sequence numbers, owner's GPS interval
constexpr uint32_t kBeaconRefreshMs = 10 * 1000; // re-apply the beacon advertisement this often
// Until a base adopts the collar it says hello on the primary channel, so the base app can list it:
// every minute for the first 15 minutes after boot, then every 15 minutes.
constexpr uint32_t kHelloFastMs = 60 * 1000, kHelloSlowMs = 15 * 60 * 1000, kHelloFastForMs = 15 * 60 * 1000;
// A frame takes about 45 ms on the air. If the RMT driver has not finished one after this long it
// is stuck, and the correction is abandoned rather than holding the collar up.
constexpr uint32_t kFrameTimeoutMs = 500;
// With fences loaded and no fix coming in, the GPS search is restarted this often (see watchGps).
constexpr uint32_t kGpsKickMs = 60 * 1000;
// When the GPS has a lock but has not reported it yet, the restart waits this long, once. Longer
// than the 20 s Meshtastic keeps a new lock before it reports the fix.
constexpr uint32_t kGpsLockWaitMs = 30 * 1000;
// At most one extra report in this long for "the GPS fix is gone" or "it is back".
constexpr uint32_t kNoFixReportMs = 5 * 60 * 1000;
// The first report after boot goes out this long after power-up, once the radio has settled.
constexpr uint32_t kBootReportMs = 15 * 1000;
// Nobody watches a collar's screen. With Meshtastic's screen timeout left at its default (10
// minutes after every button press, boot or text message on the mesh) the backlight is lit for
// this long instead. A timeout the owner sets in the Meshtastic app is left alone.
constexpr uint32_t kScreenOnSecs = 30;

uint16_t clampU16(uint32_t v)
{
    return v > UINT16_MAX ? UINT16_MAX : static_cast<uint16_t>(v);
}

uint8_t clampU8(uint32_t v)
{
    return v > UINT8_MAX ? UINT8_MAX : static_cast<uint8_t>(v);
}

/// Sends CaiXianLin frames on the data-line GPIO without blocking the Meshtastic scheduler:
/// one frame per call to poll(), each started asynchronously on the RMT peripheral.
class CollarTransmitter
{
  public:
    bool begin()
    {
        // The line idles low, so the receiver hears nothing between frames.
        ready_ = rmtInit(GEOCOLLAR_TX_PIN, RMT_TX_MODE, RMT_MEM_NUM_BLOCKS_1, 1000000); // 1 us ticks
        if (!ready_)
            LOG_ERROR("GeoCollar: RMT init failed on GPIO %d", GEOCOLLAR_TX_PIN);
        return ready_;
    }

    /// Start (or replace) a correction. durationMs is clamped to the firmware maximum, and runs
    /// from the first frame, which the next poll() sends: so call poll() soon.
    void start(CollarAction action, uint8_t intensity, uint32_t durationMs)
    {
        if (!ready_)
            return; // no transmitter: nothing to send, and nothing for the collar to wait on
        action_ = action;
        intensity_ = intensity;
        durationMs_ = durationMs > GEOCOLLAR_MAX_CORRECTION_MS ? GEOCOLLAR_MAX_CORRECTION_MS : durationMs;
        pending_ = true;
        busy_ = true;
    }

    /// Stop now: send only the stop frame (vibrate at 0) for a short while.
    void stop()
    {
        if (!ready_)
            return;
        const uint32_t now = millis();
        pending_ = false;
        activeUntil_ = now;
        stopUntil_ = now + GEOCOLLAR_STOP_MS;
        busy_ = true;
    }

    bool busy() const { return busy_; }

    /// Send the next frame if the previous one finished. Returns ms until the next poll is due.
    int32_t poll()
    {
        if (!busy_)
            return 1000;
        const uint32_t now = millis();
        if (!rmtTransmitCompleted(GEOCOLLAR_TX_PIN)) {
            if (now - frameMs_ < kFrameTimeoutMs)
                return 5;
            return abandon("the last frame never finished");
        }
        if (pending_) { // the correction's time starts with its first frame, not when it was asked for
            pending_ = false;
            activeUntil_ = now + durationMs_;
            stopUntil_ = activeUntil_ + GEOCOLLAR_STOP_MS;
        }

        CollarAction a = action_;
        uint8_t level = intensity_;
        if ((int32_t)(now - activeUntil_) >= 0) {
            if ((int32_t)(now - stopUntil_) >= 0) {
                busy_ = false; // the RMT channel idles low between frames
                return 1000;
            }
            a = CollarAction::Vibrate; // the stop frame is "vibrate at level 0"
            level = 0;
        }

        Pulse pulses[caixianlin::kFrameLength];
        if (!caixianlin::buildFrame(pulses, GEOCOLLAR_TRANSMITTER_ID, GEOCOLLAR_CHANNEL, a, level))
            return abandon("GEOCOLLAR_CHANNEL is not 0, 1 or 2");
        for (size_t i = 0; i < caixianlin::kFrameLength; i++) {
            symbols_[i].level0 = 1;
            symbols_[i].duration0 = pulses[i].highUs;
            symbols_[i].level1 = 0;
            symbols_[i].duration1 = pulses[i].lowUs;
        }
        if (!rmtWriteAsync(GEOCOLLAR_TX_PIN, symbols_, caixianlin::kFrameLength))
            return abandon("the RMT driver refused a frame");
        frameMs_ = now;
        return caixianlin::kFrameUs / 1000 + 1;
    }

  private:
    /// Give up on the current correction. The receiver acts only while frames arrive, so it stops
    /// by itself.
    int32_t abandon(const char *why)
    {
        LOG_ERROR("GeoCollar: correction abandoned, %s", why);
        busy_ = false;
        pending_ = false;
        return 1000;
    }

    bool ready_ = false;
    bool busy_ = false;
    bool pending_ = false; // start() was called and the first frame has not gone out yet
    CollarAction action_ = CollarAction::Beep;
    uint8_t intensity_ = 0;
    uint32_t durationMs_ = 0;
    uint32_t activeUntil_ = 0;
    uint32_t stopUntil_ = 0;
    uint32_t frameMs_ = 0; // when the last frame was handed to the RMT driver
    rmt_data_t symbols_[caixianlin::kFrameLength];
};

/// The Bluetooth half of the lost beacon. It borrows Meshtastic's own BLE advertising instead of
/// starting a second stack: during the "on" phase the advertisement carries only the beacon name,
/// sent once a second (the slowest usual interval, to save battery), and during the "off" phase
/// advertising stops altogether. Meshtastic's normal advertising comes back when the beacon ends.
/// Needs Bluetooth enabled in the collar's Meshtastic settings: with it disabled, Meshtastic frees
/// the Bluetooth memory at boot and it cannot be started again without a reboot.
class BeaconAdvertiser
{
  public:
    /// Call every second or so with the wanted state (nullptr name = beacon off).
    void update(const char *name, uint32_t nowMs)
    {
#if GEOCOLLAR_HAS_BLE
        const bool ble = nimbleBluetooth && nimbleBluetooth->isActive();
        if (!ble) {
            if (name && !warned_) {
                LOG_WARN("GeoCollar: lost beacon has no Bluetooth (enable Bluetooth in the Meshtastic settings)");
                warned_ = true;
            }
            mode_ = Mode::Normal;
            return;
        }
        if (nimbleBluetooth->isConnected())
            return; // the owner's phone is connected: leave Bluetooth alone
        if (!name) {
            if (mode_ != Mode::Normal) {
                // Meshtastic's own routine: it clears the beacon's name and interval and starts
                // its normal advertising again.
                nimbleBluetooth->startAdvertising();
                if (!BLEDevice::getAdvertising()->isAdvertising())
                    return; // the Bluetooth stack is restarting: try again on the next pass
                mode_ = Mode::Normal;
                LOG_INFO("GeoCollar: lost beacon Bluetooth off, normal advertising back");
            }
            return;
        }
        if (name[0] == 0) { // the off half of the cycle
            if (mode_ != Mode::Off) {
                BLEDevice::getAdvertising()->stop();
                mode_ = Mode::Off;
            }
            return;
        }
        // Re-apply now and then, in case Meshtastic restarted its own advertising (e.g. after a
        // phone disconnected).
        if (mode_ == Mode::Beacon && strcmp(name, name_) == 0 && nowMs - appliedMs_ < kBeaconRefreshMs)
            return;
        BLEAdvertising *adv = BLEDevice::getAdvertising();
        adv->stop();
        adv->reset();
        char primary[27];
        strncpy(primary, name, sizeof(primary) - 1); // 31 bytes less flags and the name header
        primary[sizeof(primary) - 1] = 0;
        BLEAdvertisementData data;
        data.setFlags(0x06); // general discoverable, no classic Bluetooth
        data.setName(primary);
        adv->setAdvertisementData(data);
        BLEAdvertisementData scan;
        scan.setName(name);
        adv->setScanResponseData(scan);
        adv->setMinInterval(1600); // 1 s, in 0.625 ms units
        adv->setMaxInterval(1600);
        if (!adv->start(0))
            LOG_ERROR("GeoCollar: lost beacon advertising failed"); // tried again in kBeaconRefreshMs
        else if (mode_ != Mode::Beacon)
            LOG_INFO("GeoCollar: lost beacon advertising \"%s\"", name);
        strncpy(name_, name, sizeof(name_) - 1);
        name_[sizeof(name_) - 1] = 0;
        appliedMs_ = nowMs;
        mode_ = Mode::Beacon;
#else
        (void)nowMs;
        if (name && !warned_) {
            LOG_WARN("GeoCollar: lost beacon has no Bluetooth in this build");
            warned_ = true;
        }
#endif
    }

  private:
    enum class Mode : uint8_t { Normal, Beacon, Off };
    Mode mode_ = Mode::Normal;
    bool warned_ = false;
    char name_[32] = {};
    uint32_t appliedMs_ = 0;
};

class GeoCollarModule : public SinglePortModule, private concurrency::OSThread
{
  public:
    GeoCollarModule()
        : SinglePortModule("geocollar", static_cast<meshtastic_PortNum>(proto::kPortNum)), OSThread("GeoCollar")
    {
        if (GEOCOLLAR_DISABLE_PIN >= 0)
            pinMode(GEOCOLLAR_DISABLE_PIN, INPUT_PULLUP);
        tx_.begin();
        loadState();
        // Runs before Meshtastic sets up its screen timer, so this is the timeout it uses. Set in
        // RAM; Meshtastic writes it to flash the next time it saves its settings, which changes
        // nothing: it is replaced here only while it is still the default.
        if (config.display.screen_on_secs == 0)
            config.display.screen_on_secs = kScreenOnSecs;
        LOG_INFO("GeoCollar: ready, fence v%u, %u zones", engine_.fences().version, engine_.fences().zoneCount);
    }

  protected:
    // Only accept GeoCollar packets that are PKI direct messages from an authorized admin key,
    // i.e. the owner's base station. Everything else on our port is ignored.
    ProcessMessage handleReceived(const meshtastic_MeshPacket &mp) override
    {
        if (isFromUs(&mp))
            return ProcessMessage::CONTINUE;
        if (!mp.pki_encrypted || !AdminKeys::isAuthorized(mp.public_key.bytes)) {
            // Other collars' hellos land here too, so only a direct message is worth a warning.
            if (isToUs(&mp))
                LOG_WARN("GeoCollar: ignoring packet from 0x%08x (not an authorized base)", mp.from);
            return ProcessMessage::STOP;
        }
        // One write to flash per message at most, and none when nothing changed.
        bool save = false;
        if (baseNode_ != mp.from) {
            baseNode_ = mp.from;
            save = true; // so a restart still knows where to report
        }

        const uint8_t *buf = mp.decoded.payload.bytes;
        const size_t len = mp.decoded.payload.size;
        proto::MsgType type;
        if (!proto::messageType(buf, len, type)) {
            if (save)
                saveState();
            return ProcessMessage::STOP;
        }

        if (type == proto::MsgType::FenceChunk) {
            proto::FenceChunk chunk;
            if (!proto::decodeFenceChunk(buf, len, chunk)) {
                LOG_WARN("GeoCollar: malformed fence chunk");
            } else if (!proto::fenceVersionAcceptable(chunk.fenceVersion, engine_.fences().version)) {
                // Refused before it reaches the assembler, so a recording of old fences cannot
                // throw away a newer set that is still arriving.
                LOG_WARN("GeoCollar: refusing fence v%u, older than v%u (a replay?)", chunk.fenceVersion,
                         engine_.fences().version);
            } else if (assembler_.add(chunk)) {
                const uint16_t version = assembler_.version();
                if (!proto::decodeFenceSet(assembler_.data(), assembler_.length(), version, scratch_)) {
                    LOG_WARN("GeoCollar: fence v%u is malformed", version);
                } else {
                    switch (proto::fenceUpdate(version, scratch_, engine_.fences())) {
                    case proto::FenceUpdate::Refuse:
                        LOG_WARN("GeoCollar: refusing fence v%u, older than v%u (a replay?)", version,
                                 engine_.fences().version);
                        break;
                    case proto::FenceUpdate::Keep:
                        // Loading the same fences again would restart the engine, and with it the
                        // shock limits and the lost beacon. Just say that they are in force.
                        LOG_INFO("GeoCollar: fence v%u is already loaded", version);
                        sendStatus(statusType());
                        break;
                    case proto::FenceUpdate::Load:
                        engine_.setFences(scratch_);
                        pulser_.stop();
                        beacon_.reset();
                        // Where the last fix stands against the new lines, for the report and for
                        // the GPS pacing; with no current fix, 0 makes the GPS fix often.
                        lastClearance_ = (scratch_.zoneCount && !fixWatch_.noFix())
                                             ? clearanceM(engine_.fences(), lastFix_.latI, lastFix_.lonI)
                                             : 0;
                        save = true;
                        LOG_INFO("GeoCollar: fence v%u loaded, %u zones", scratch_.version, scratch_.zoneCount);
                        sendStatus(proto::MsgType::Status);
                        break;
                    }
                }
            }
        } else if (type == proto::MsgType::Command) {
            proto::Command cmd;
            if (!proto::decodeCommand(buf, len, cmd)) {
                LOG_WARN("GeoCollar: malformed command");
            } else if (cmd.seq <= lastCmdSeq_) {
                LOG_WARN("GeoCollar: refusing command %u, not newer than %u (a replay?)", (unsigned)cmd.seq,
                         (unsigned)lastCmdSeq_);
            } else {
                lastCmdSeq_ = cmd.seq;
                handleCommand(cmd);
                save = true; // the number at least: a restart must not forget which commands it has seen
            }
        } else if (type == proto::MsgType::Owner) {
            proto::OwnerInfo o;
            uint32_t seq = 0;
            if (!proto::decodeOwner(buf, len, o, seq)) {
                LOG_WARN("GeoCollar: malformed owner message");
            } else if (!proto::settingSeqAcceptable(seq, lastOwnerSeq_)) {
                LOG_WARN("GeoCollar: refusing owner message %u, not newer than %u (a replay?)", (unsigned)seq,
                         (unsigned)lastOwnerSeq_);
            } else {
                const bool changed = strcmp(o.dog, owner_.dog) != 0 || strcmp(o.owner, owner_.owner) != 0 ||
                                     strcmp(o.phone, owner_.phone) != 0;
                if (changed || seq != lastOwnerSeq_)
                    save = true;
                owner_ = o;
                lastOwnerSeq_ = seq;
                LOG_INFO("GeoCollar: dog's name \"%s\", owner contact set", owner_.dog);
                applyMeshName();
                sendStatus(statusType());
            }
        } else if (type == proto::MsgType::Config) {
            proto::CollarConfig c;
            uint32_t seq = 0;
            if (!proto::decodeConfig(buf, len, c, seq)) {
                LOG_WARN("GeoCollar: malformed settings message");
            } else if (!proto::settingSeqAcceptable(seq, lastConfigSeq_)) {
                LOG_WARN("GeoCollar: refusing settings message %u, not newer than %u (a replay?)", (unsigned)seq,
                         (unsigned)lastConfigSeq_);
            } else {
                if (c.reportS != reportS_ || seq != lastConfigSeq_)
                    save = true;
                reportS_ = c.reportS;
                lastConfigSeq_ = seq;
                LOG_INFO("GeoCollar: report every %u s", (unsigned)(reportS_ ? reportS_ : GEOCOLLAR_STATUS_INTERVAL_MS / 1000));
            }
        }
        if (save)
            saveState();
        if (tx_.busy())
            setIntervalFromNow(0); // a command started the transmitter: send its first frame now
        return ProcessMessage::STOP;
    }

    int32_t runOnce() override
    {
        if (!named_) {
            named_ = true;
            nameFreshCollar();
        }
        watchForFixes();

        checkPosition(); // a new fix may start a correction, or stop one that is playing
        if (!tx_.busy()) {
            // The rest waits while frames are going out, so that they follow each other closely.
            repeatPulse();
            runBeacon();
            const uint32_t now = millis();
            watchGps(now);
            applyGpsPacing();
            report(now);
        }
        // Send the next frame, or the first one of a correction started above.
        return tx_.busy() ? tx_.poll() : 1000;
    }

  private:
    /// Breach while the dog is lost, Status otherwise: the base alerts on a breach.
    proto::MsgType statusType() const
    {
        return engine_.state() == CollarState::Lost ? proto::MsgType::Breach : proto::MsgType::Status;
    }

    bool hardwareDisabled() const { return GEOCOLLAR_DISABLE_PIN >= 0 && digitalRead(GEOCOLLAR_DISABLE_PIN) == LOW; }

    /// Hellos until a base adopts the collar, then reports to that base.
    void report(uint32_t now)
    {
        if (helloFast_ && now >= kHelloFastForMs)
            helloFast_ = false; // a flag, so the fast hellos do not come back when millis() wraps
        if (!baseNode_) {
            if (lastHelloMs_ == 0 || now - lastHelloMs_ >= (helloFast_ ? kHelloFastMs : kHelloSlowMs))
                sendHello();
            return;
        }
        // The owner's report interval from the base; while lost, at least every 30 s.
        const uint32_t normal = reportS_ ? reportS_ * 1000u : GEOCOLLAR_STATUS_INTERVAL_MS;
        const uint32_t interval = engine_.state() == CollarState::Lost && normal > GEOCOLLAR_LOST_STATUS_INTERVAL_MS
                                      ? GEOCOLLAR_LOST_STATUS_INTERVAL_MS
                                      : normal;
        // After a restart the base hears from the collar within seconds, not a whole interval later.
        const bool bootReport = !bootReported_ && now >= kBootReportMs;
        if (bootReport || now - lastStatusMs_ >= interval)
            sendStatus(statusType());
    }

#if GEOCOLLAR_HAS_GPS
    /// Meshtastic's GPS calls this with every new reading and when it loses its lock. The fence
    /// engine takes fixes only from here, the collar's own GPS: Meshtastic's stored position can
    /// also come from a phone app sharing its location with the collar, or be an old position
    /// restored from flash, and neither says where the dog is now.
    int onGpsStatus(const meshtastic::GPSStatus *status)
    {
        if (!gps || !status->getHasLock() || config.position.fixed_position)
            return 0;
        const meshtastic_Position &p = gps->p;
        if (p.timestamp == 0 || p.timestamp == lastFixTimestamp_ || (p.latitude_i == 0 && p.longitude_i == 0))
            return 0; // not a new fix (the GPS also publishes when it only has the time)
        lastFixTimestamp_ = p.timestamp;
        pendingFix_ = Fix{p.latitude_i, p.longitude_i, clampU16(p.PDOP), clampU8(p.sats_in_view), millis()};
        fixPending_ = true;
        setIntervalFromNow(0); // check the fences now, not at the next once-a-second pass
        return 0;
    }
#endif

    /// Subscribe to the GPS once it exists.
    void watchForFixes()
    {
#if GEOCOLLAR_HAS_GPS
        if (!gpsObserved_ && gps) {
            gpsObserver_.observe(&gps->newStatus);
            gpsObserved_ = true;
        }
#endif
    }

    void checkPosition()
    {
        if (!fixPending_)
            return;
        fixPending_ = false;
        const Fix fix = pendingFix_;
        lastFix_ = fix;
        fixWatch_.onFix(fix.nowMs); // before any report below, so that it says the position is current

        const bool savedEnable = engine_.settings().correctionsEnabled;
        if (hardwareDisabled())
            engine_.settings().correctionsEnabled = false;
        const Decision d = engine_.onFix(fix);
        engine_.settings().correctionsEnabled = savedEnable;

        lastClearance_ = d.clearanceM; // 0 for a fix too poor to use, which also makes the GPS fix often
        if (!d.fixRejected)
            beacon_.onFix(engine_.state() != CollarState::NoFence && d.clearanceM < 0, fix.nowMs);
        // Past the line every correction stops at once, including one that is still playing.
        const CollarState st = engine_.state();
        if (autoCorrection_ && tx_.busy() && (st == CollarState::Outside || st == CollarState::Lost)) {
            tx_.stop();
            autoCorrection_ = false;
        }
        if (pulser_.onDecision(d, engine_.fences(), fix.nowMs))
            applyCorrection(d.correction, d.intensity);
        if (d.sendBreach)
            sendStatus(proto::MsgType::Breach);
        else if (d.sendStatus)
            sendStatus(proto::MsgType::Status);
    }

    /// Keeps track of whether the collar has a current fix, tells the base when that changes, and
    /// keeps the GPS searching. Meshtastic gives up after 15 minutes without a lock and then
    /// looks again only once per position broadcast interval (15 minutes by default, an hour at
    /// most), which would leave a dog that has come back out from under a roof unfenced for that
    /// long. With fences loaded the search is restarted every minute instead. Where Meshtastic can
    /// switch the GPS off, that keeps it powered while the dog is indoors; on the Wireless Tracker
    /// the GPS is powered all the time anyway.
    void watchGps(uint32_t now)
    {
        const bool noFix = fixWatch_.tick(now, config.position.gps_update_interval);
        if (noFix != loggedNoFix_) {
            loggedNoFix_ = noFix;
            LOG_INFO("GeoCollar: %s", noFix ? "no GPS fix" : "GPS fix");
        }
        if (engine_.state() == CollarState::NoFence)
            return; // nothing to guard: leave the GPS to the owner's Meshtastic settings
        if (baseNode_ && noFix != reportedNoFix_ && (lastNoFixReportMs_ == 0 || now - lastNoFixReportMs_ >= kNoFixReportMs)) {
            lastNoFixReportMs_ = now | 1;
            sendStatus(statusType());
        }
#if GEOCOLLAR_HAS_GPS
        if (!noFix) {
            kickWaited_ = false;
        } else if (gps && config.position.gps_mode == meshtastic_Config_PositionConfig_GpsMode_ENABLED &&
                   !config.position.fixed_position && now - lastGpsKickMs_ >= kGpsKickMs) {
            if (gps->hasLock() && !kickWaited_) {
                // The GPS has just locked and is about to report the fix. A restart in the moment
                // it finishes would make Meshtastic count the search as failed and put the GPS
                // to sleep for 15 minutes or more, so look again a little later.
                kickWaited_ = true;
                lastGpsKickMs_ = now - kGpsKickMs + kGpsLockWaitMs;
            } else {
                kickWaited_ = false;
                lastGpsKickMs_ = now;
                gps->up(); // start a new search, or keep the running one from timing out
            }
        }
#endif
    }

    // GPS pacing. Meshtastic's GPS thread reads config.position.gps_update_interval on every pass,
    // so changing it in RAM takes effect within about 5 s. The owner's own value is remembered and
    // put back when there are no fences or pacing is off; if the owner changes the interval in the
    // Meshtastic app or over the mesh, that becomes the new value to put back.
    //
    // The owner's value is kept in the collar's own state file. Meshtastic writes its whole config
    // to flash when it shuts down, goes to deep sleep or has any setting changed, the interval set
    // here included, so after a restart the config may hold one of the collar's own values.
    void applyGpsPacing()
    {
        uint32_t &current = config.position.gps_update_interval;
        if (lastPacedSecs_ == UINT32_MAX) { // the first pass since boot
            if (!ownerGpsKnown_) {
                ownerGpsSecs_ = current; // nothing saved yet: no fences have been paced, so this is the owner's
                ownerGpsKnown_ = true;
            }
        } else if (current != lastPacedSecs_) {
            ownerGpsSecs_ = current;
            saveState();
        }
        const uint32_t want = gpsIntervalSecs(engine_.settings(), engine_.state(), lastClearance_,
                                              alertWidthM(engine_.fences()), beacon_.active());
        const uint32_t target = want ? want : ownerGpsSecs_;
        if (current != target) {
            LOG_DEBUG("GeoCollar: GPS interval %us -> %us", (unsigned)current, (unsigned)target);
            current = target;
        }
        lastPacedSecs_ = current;
    }

    /// The lost-dog beacon: Bluetooth name on and off in 90 s turns, and a plain-text plea on the
    /// mesh every 5 minutes, from 90 s after the dog went past a line until it is back inside or the
    /// owner silences it.
    void runBeacon()
    {
        const uint32_t now = millis();
        const bool wasActive = beacon_.active();
        beacon_.tick(now);
        const bool active = beacon_.active();
        if (active != wasActive) {
            LOG_INFO("GeoCollar: lost beacon %s", active ? "on" : "off");
            sendStatus(statusType());
        }
        if (!active) {
            ble_.update(nullptr, now);
            return;
        }
        char name[30];
        if (beacon_.bleOn(now))
            beaconName(owner_.dog[0] ? owner_.dog : "Dog", owner_.phone, name, sizeof(name));
        else
            name[0] = 0;
        ble_.update(name, now);
        if (beacon_.meshDue(now))
            sendPlea();
    }

    /// Broadcast the plea as an ordinary text message on a channel strangers can read: the first one
    /// with the well-known default key or no key. With none, it goes on the primary channel, which
    /// only nodes sharing that channel can read.
    void sendPlea()
    {
        char text[200];
        const size_t n = beaconText(owner_.dog[0] ? owner_.dog : "Dog", owner_.owner, owner_.phone, lastFix_.latI,
                                    lastFix_.lonI, text, sizeof(text));
        meshtastic_MeshPacket *p = allocDataPacket();
        if (!p || n > sizeof(p->decoded.payload.bytes)) {
            if (p)
                packetPool.release(p);
            return;
        }
        p->to = NODENUM_BROADCAST;
        p->channel = publicChannel();
        p->want_ack = false;
        p->decoded.portnum = meshtastic_PortNum_TEXT_MESSAGE_APP;
        memcpy(p->decoded.payload.bytes, text, n);
        p->decoded.payload.size = n;
        LOG_INFO("GeoCollar: lost plea on channel %u: %s", (unsigned)p->channel, text);
        service->sendToMesh(p, RX_SRC_LOCAL, false);
    }

    static uint8_t publicChannel()
    {
        for (uint8_t i = 0; i < channels.getNumChannels(); i++) {
            const meshtastic_Channel &ch = channels.getByIndex(i);
            if (ch.role == meshtastic_Channel_Role_DISABLED)
                continue;
            const auto &psk = ch.settings.psk;
            if (psk.size == 0 || (psk.size == 1 && psk.bytes[0] == 1))
                return i;
        }
        return channels.getPrimaryIndex();
    }

    /// Repeat beep / vibrate between fixes at the pulse interval sent with the fences.
    void repeatPulse()
    {
        const bool allowed =
            engine_.settings().correctionsEnabled && !hardwareDisabled() && engine_.state() == CollarState::Warning;
        uint8_t intensity = 0;
        const Correction c = pulser_.poll(engine_.fences(), millis(), allowed, intensity);
        if (c != Correction::None)
            applyCorrection(c, intensity);
    }

    void applyCorrection(Correction c, uint8_t intensity)
    {
        const uint32_t ms = engine_.settings().correctionMs;
        if (c != Correction::None)
            autoCorrection_ = true; // the fence's own doing: cut short if the dog goes past the line
        switch (c) {
        case Correction::Beep:
            tx_.start(CollarAction::Beep, 0, ms);
            break;
        case Correction::Vibrate:
            tx_.start(CollarAction::Vibrate, intensity, ms);
            break;
        case Correction::Shock:
            tx_.start(CollarAction::Shock, intensity, ms);
            break;
        case Correction::None:
            break;
        }
    }

    /// A command from the base. The caller saves state afterwards.
    void handleCommand(const proto::Command &cmd)
    {
        Settings &s = engine_.settings();
        // The hardware switch turns off every correction, the owner's own included.
        const bool manual = cmd.action == proto::CommandAction::Beep || cmd.action == proto::CommandAction::Vibrate ||
                            cmd.action == proto::CommandAction::Shock;
        if (manual && hardwareDisabled()) {
            LOG_WARN("GeoCollar: command ignored, the hardware switch has corrections off");
            sendStatus(statusType());
            return;
        }
        if (manual)
            autoCorrection_ = false; // the owner's own: plays out wherever the dog is
        switch (cmd.action) {
        case proto::CommandAction::Beep:
            tx_.start(CollarAction::Beep, 0, cmd.durationMs);
            break;
        case proto::CommandAction::Vibrate:
            tx_.start(CollarAction::Vibrate, cmd.intensity > 99 ? 99 : cmd.intensity, cmd.durationMs);
            break;
        case proto::CommandAction::Shock:
            // Manual shock from the owner still respects the level cap and the shock switch.
            if (s.shockEnabled)
                tx_.start(CollarAction::Shock, cmd.intensity < s.maxShockLevel ? cmd.intensity : s.maxShockLevel,
                          cmd.durationMs);
            break;
        case proto::CommandAction::CorrectionsOff:
            s.correctionsEnabled = false;
            tx_.stop();
            pulser_.stop();
            break;
        case proto::CommandAction::CorrectionsOn:
            s.correctionsEnabled = true;
            break;
        case proto::CommandAction::ForceLost:
            engine_.forceLost();
            tx_.stop();
            pulser_.stop();
            break;
        case proto::CommandAction::ClearLost:
            engine_.clearLost();
            break;
        case proto::CommandAction::RequestStatus:
            break;
        case proto::CommandAction::ShockOn:
            // The owner turned shock on in the base app (after confirming). The level is capped by
            // the firmware's maximum, and the per-breach and per-hour limits still apply.
            if (cmd.intensity >= 1) {
                s.shockEnabled = true;
                s.shockLevel = cmd.intensity < s.maxShockLevel ? cmd.intensity : s.maxShockLevel;
                LOG_INFO("GeoCollar: shock on, level %u", s.shockLevel);
            }
            break;
        case proto::CommandAction::ShockOff:
            s.shockEnabled = false;
            LOG_INFO("GeoCollar: shock off");
            break;
        case proto::CommandAction::SilenceLost:
            beacon_.silence();
            ble_.update(nullptr, millis());
            break;
        }
        sendStatus(statusType());
    }

    /// A freshly installed collar has Meshtastic's default name, "Meshtastic 1a2b". Call it
    /// "GeoCollar 1a2b" instead, so it is recognisable in the Meshtastic app, in Bluetooth scans and
    /// on the base page until the base names it after the dog. Runs once, from the first runOnce(),
    /// when every other module (NodeInfo in particular) exists.
    void nameFreshCollar()
    {
        if (!nodeDB || !service || applyMeshName())
            return;
        const unsigned suffix = nodeDB->getNodeNum() & 0xffff;
        char stock[sizeof(owner.long_name)];
        snprintf(stock, sizeof(stock), "Meshtastic %04x", suffix);
        if (strcmp(owner.long_name, stock) != 0)
            return;
        snprintf(owner.long_name, sizeof(owner.long_name), "GeoCollar %04x", suffix);
        service->reloadOwner(false);
        nodeDB->saveToDisk(SEGMENT_DEVICESTATE | SEGMENT_NODEDATABASE);
        LOG_INFO("GeoCollar: named this collar \"%s\"", owner.long_name);
    }

    /// The collar's Meshtastic name follows the dog's name from the base ("Rex", short name "REX"),
    /// so it shows as the dog in the Meshtastic app and on other nodes. Returns true if it changed.
    bool applyMeshName()
    {
        if (!owner_.dog[0] || !nodeDB || !service)
            return false;
        char longName[sizeof(owner.long_name)], shortName[sizeof(owner.short_name)];
        proto::meshNames(owner_.dog, longName, sizeof(longName), shortName, sizeof(shortName));
        if (!longName[0])
            return false;
        const bool sameShort = !shortName[0] || strcmp(owner.short_name, shortName) == 0;
        if (strcmp(owner.long_name, longName) == 0 && sameShort)
            return false;
        memcpy(owner.long_name, longName, sizeof(longName));
        if (shortName[0])
            memcpy(owner.short_name, shortName, sizeof(shortName));
        service->reloadOwner(true); // and tell the mesh, so the base and phones show the new name
        nodeDB->saveToDisk(SEGMENT_DEVICESTATE | SEGMENT_NODEDATABASE);
        LOG_INFO("GeoCollar: Meshtastic name is now \"%s\" (%s)", owner.long_name, owner.short_name);
        return true;
    }

    void sendHello()
    {
        lastHelloMs_ = millis() | 1;
        meshtastic_MeshPacket *p = allocDataPacket();
        if (!p)
            return;
        p->to = NODENUM_BROADCAST;
        p->want_ack = false;
        const uint8_t battery = powerStatus ? powerStatus->getBatteryChargePercent() : 255;
        p->decoded.payload.size = proto::encodeHello(p->decoded.payload.bytes, sizeof(p->decoded.payload.bytes),
                                                     config.security.admin_key_count > 0, engine_.fences().zoneCount > 0, battery);
        LOG_INFO("GeoCollar: hello, waiting for a base");
        service->sendToMesh(p, RX_SRC_LOCAL, false);
    }

    void sendStatus(proto::MsgType type)
    {
        if (!baseNode_)
            return;
        proto::StatusReport s{};
        s.fenceVersion = engine_.fences().version;
        s.state = static_cast<uint8_t>(engine_.state());
        s.batteryPct = powerStatus ? powerStatus->getBatteryChargePercent() : 255;
        s.latI = lastFix_.latI;
        s.lonI = lastFix_.lonI;
        s.pdopX100 = lastFix_.pdopX100;
        s.sats = lastFix_.sats;
        const float c = lastClearance_;
        s.clearanceM = c > 32767 ? 32767 : (c < -32768 ? -32768 : (int16_t)c);
        s.flags = (engine_.settings().correctionsEnabled ? proto::kFlagCorrections : 0) |
                  (engine_.settings().shockEnabled ? proto::kFlagShock : 0) | (beacon_.active() ? proto::kFlagBeacon : 0) |
                  (beacon_.silenced() ? proto::kFlagBeaconSilenced : 0) | (fixWatch_.noFix() ? proto::kFlagNoFix : 0) |
                  proto::kFlagNumbered;

        meshtastic_MeshPacket *p = allocDataPacket();
        if (!p)
            return;
        p->to = baseNode_; // a direct message: Meshtastic encrypts it with PKI when it knows the key
        p->want_ack = true;
        p->decoded.payload.size =
            proto::encodeStatus(p->decoded.payload.bytes, sizeof(p->decoded.payload.bytes), type, s);
        service->sendToMesh(p, RX_SRC_LOCAL, false);
        lastStatusMs_ = millis();
        bootReported_ = true;
        reportedNoFix_ = fixWatch_.noFix();
    }

    // Persistence: [magic u32][format u16][base node u32][fence version u16][blob len u16][blob][Settings]
    // [OwnerInfo][command seq u32][report interval s u16][owner seq u32][config seq u32]
    // [owner's GPS interval s u32][that value is known u8]
    void saveState()
    {
        const size_t blobLen = proto::encodeFenceSet(blob_, sizeof(blob_), engine_.fences());
        const uint16_t version = engine_.fences().version;
        const uint16_t len16 = blobLen;
        {
            concurrency::LockGuard g(spiLock);
            FSCom.mkdir("/prefs");
        }
        SafeFile f(kStateFile, true);
        f.write((const uint8_t *)&kStateMagic, sizeof(kStateMagic));
        f.write((const uint8_t *)&kStateFormat, sizeof(kStateFormat));
        f.write((const uint8_t *)&baseNode_, sizeof(baseNode_));
        f.write((const uint8_t *)&version, sizeof(version));
        f.write((const uint8_t *)&len16, sizeof(len16));
        f.write(blob_, blobLen);
        f.write((const uint8_t *)&engine_.settings(), sizeof(Settings));
        f.write((const uint8_t *)&owner_, sizeof(owner_));
        f.write((const uint8_t *)&lastCmdSeq_, sizeof(lastCmdSeq_));
        f.write((const uint8_t *)&reportS_, sizeof(reportS_));
        f.write((const uint8_t *)&lastOwnerSeq_, sizeof(lastOwnerSeq_));
        f.write((const uint8_t *)&lastConfigSeq_, sizeof(lastConfigSeq_));
        const uint8_t gpsKnown = ownerGpsKnown_;
        f.write((const uint8_t *)&ownerGpsSecs_, sizeof(ownerGpsSecs_));
        f.write(&gpsKnown, sizeof(gpsKnown));
        if (!f.close())
            LOG_ERROR("GeoCollar: failed to save %s", kStateFile);
    }

    void loadState()
    {
        concurrency::LockGuard g(spiLock);
        if (!FSCom.exists(kStateFile))
            return;
        auto f = FSCom.open(kStateFile, FILE_O_READ);
        if (!f)
            return;
        uint32_t magic = 0;
        uint16_t format = 0, version = 0, len16 = 0;
        uint32_t base = 0;
        Settings settings;
        proto::OwnerInfo owner;
        uint32_t seq = 0, ownerSeq = 0, configSeq = 0, gpsSecs = 0;
        uint16_t reportS = 0;
        uint8_t gpsKnown = 0;
        bool ok = f.read((uint8_t *)&magic, sizeof(magic)) == sizeof(magic) && magic == kStateMagic &&
                  f.read((uint8_t *)&format, sizeof(format)) == sizeof(format) && format >= 4 && format <= kStateFormat &&
                  f.read((uint8_t *)&base, sizeof(base)) == sizeof(base) &&
                  f.read((uint8_t *)&version, sizeof(version)) == sizeof(version) &&
                  f.read((uint8_t *)&len16, sizeof(len16)) == sizeof(len16) && len16 <= sizeof(blob_) &&
                  f.read(blob_, len16) == len16 && f.read((uint8_t *)&settings, sizeof(settings)) == sizeof(settings) &&
                  (format < 5 || f.read((uint8_t *)&owner, sizeof(owner)) == sizeof(owner)) && // 4: no owner yet
                  (format < 6 || f.read((uint8_t *)&seq, sizeof(seq)) == sizeof(seq)) &&
                  (format < 7 || f.read((uint8_t *)&reportS, sizeof(reportS)) == sizeof(reportS)) &&
                  (format < 8 || (f.read((uint8_t *)&ownerSeq, sizeof(ownerSeq)) == sizeof(ownerSeq) &&
                                  f.read((uint8_t *)&configSeq, sizeof(configSeq)) == sizeof(configSeq) &&
                                  f.read((uint8_t *)&gpsSecs, sizeof(gpsSecs)) == sizeof(gpsSecs) &&
                                  f.read(&gpsKnown, sizeof(gpsKnown)) == sizeof(gpsKnown)));
        f.close();
        if (ok && proto::decodeFenceSet(blob_, len16, version, scratch_)) {
            engine_.setFences(scratch_);
            sanitizeSettings(settings);
            engine_.settings() = settings;
            baseNode_ = base;
            owner.dog[sizeof(owner.dog) - 1] = owner.owner[sizeof(owner.owner) - 1] = owner.phone[sizeof(owner.phone) - 1] = 0;
            owner_ = owner;
            lastCmdSeq_ = seq;
            lastOwnerSeq_ = ownerSeq;
            lastConfigSeq_ = configSeq;
            ownerGpsSecs_ = gpsSecs;
            ownerGpsKnown_ = gpsKnown != 0;
            reportS_ = (reportS == 0 || (reportS >= proto::kMinReportS && reportS <= proto::kMaxReportS)) ? reportS : 0;
        } else {
            LOG_WARN("GeoCollar: saved state unreadable, starting without fences");
        }
    }

    FenceEngine engine_;
    CorrectionPulser pulser_;
    CollarTransmitter tx_;
    LostBeacon beacon_;
    BeaconAdvertiser ble_;
    proto::OwnerInfo owner_;
    proto::FenceAssembler assembler_;
    // Work space for a fence set being decoded and for the saved-state file. Members, not locals:
    // together they are over 2 kB, too much for the main loop's stack on top of Meshtastic's own
    // call chain.
    FenceSet scratch_;
    uint8_t blob_[proto::kMaxFenceBlob] = {};
#if GEOCOLLAR_HAS_GPS
    CallbackObserver<GeoCollarModule, const meshtastic::GPSStatus *> gpsObserver_{this, &GeoCollarModule::onGpsStatus};
    bool gpsObserved_ = false;
#endif
    NodeNum baseNode_ = 0;
    uint32_t lastFixTimestamp_ = 0; // GPS time of the newest fix taken, to tell a new fix from a repeat
    Fix pendingFix_{};              // the newest fix from the GPS, waiting for runOnce()
    bool fixPending_ = false;
    Fix lastFix_{};
    FixWatch fixWatch_;         // no current fix: none since boot, or the last one is overdue
    bool loggedNoFix_ = true;
    bool reportedNoFix_ = true; // what the last report told the base about that
    uint32_t lastNoFixReportMs_ = 0;
    uint32_t lastGpsKickMs_ = 0;
    bool kickWaited_ = false; // the last restart was put off because the GPS had a lock
    float lastClearance_ = 0;
    bool autoCorrection_ = false; // the correction playing came from the fence, not from the owner
    uint32_t ownerGpsSecs_ = 0;   // the owner's own GPS interval, put back when the fences do not pace it
    bool ownerGpsKnown_ = false;  // read from the state file, or recorded on the first pass
    uint32_t lastPacedSecs_ = UINT32_MAX; // nothing set yet: the first pass records the owner's value
    uint32_t lastStatusMs_ = 0;
    bool bootReported_ = false;
    uint32_t lastHelloMs_ = 0;
    bool helloFast_ = true;
    bool named_ = false;
    uint32_t lastCmdSeq_ = 0;    // newest command accepted; older or repeated ones are replays
    uint32_t lastOwnerSeq_ = 0;  // the same for Owner messages (0: none numbered yet)
    uint32_t lastConfigSeq_ = 0; // and for Config messages
    uint16_t reportS_ = 0;       // report interval from the base, s; 0 = GEOCOLLAR_STATUS_INTERVAL_MS
};

GeoCollarModule *geoCollarModule = nullptr;

} // namespace

void setupGeoCollar()
{
    geoCollarModule = new GeoCollarModule();
}

#else

#include "GeoCollar.h"
void setupGeoCollar() {} // GeoCollar needs the ESP32 RMT peripheral

#endif
