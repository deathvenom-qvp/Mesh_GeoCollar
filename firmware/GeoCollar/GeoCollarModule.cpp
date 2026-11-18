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

#include "AppLink.h"
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
#include "main.h" // rebootAtMsec
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
#include <esp_random.h>

#if !defined(CONFIG_IDF_TARGET_ESP32S2) && !MESHTASTIC_EXCLUDE_BLUETOOTH
#include "main.h"
#include "nimble/NimbleBluetooth.h"
#include <BLEAdvertising.h>
#include <BLEDevice.h>
#include <atomic>
#include <climits>
#include "host/ble_gap.h"
#include "host/ble_hs.h"
#include "host/ble_hs_adv.h"
#define GEOCOLLAR_HAS_BLE 1
#else
#define GEOCOLLAR_HAS_BLE 0
#endif

using namespace geocollar;

namespace
{

constexpr const char *kStateFile = "/prefs/geocollar.bin";
// The home anchor, in a file of its own: [magic u32][format u8][kind u8][address 6][RSSI dBm i8]
constexpr const char *kHomeFile = "/prefs/geocollar-home.bin";
constexpr uint32_t kHomeMagic = 0x4D484347; // "GCHM"
constexpr uint8_t kHomeFormat = 1;
// Listening for the anchor: a 100 ms window every 2 s while home (so leaving is noticed within
// HomeWatch::kLeaveMs), every 5 s while away. In 0.625 ms units.
constexpr uint16_t kScanWindow = 160, kScanHomeItvl = 3200, kScanAwayItvl = 8000;
constexpr uint32_t kStateMagic = 0x47434F4C; // "GCOL"
constexpr uint16_t kStateFormat = 10; // 2: GPS pacing; 3: per-zone bands; 4: pulse intervals; 5: owner; 6: command seq;
                                      // 7: report interval; 8: owner and config sequence numbers, owner's GPS interval;
                                      // 9: companion app keys; 10: GPS interval everywhere inside
constexpr uint32_t kBeaconRefreshMs = 10 * 1000; // re-apply the beacon advertisement this often
// With a companion app linked, the beacon's quiet half still advertises the collar's ordinary
// name this often (2 s, in 0.625 ms units), so the owner's phone can connect at any moment.
constexpr uint16_t kQuietAdvInterval = 3200;
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
// Bluetooth advertising, slowed (GEOCOLLAR_BLE_ADV_MS), is applied again this often, in case
// Meshtastic restarted its own fast advertising in the meantime.
constexpr uint32_t kSlowAdvRefreshMs = 60 * 1000;
// Nobody watches a collar's screen. Versions 0.16 to 0.19 cut Meshtastic's default timeout (10
// minutes after every button press, boot or text message) to this; it now counts as a default
// too, and both give way to GEOCOLLAR_SCREEN_ON_SECS (see applyPowerSettings).
constexpr uint32_t kScreenOnSecs = 30;
// While a correction waits for its second look and the GPS has no new reading yet, look for one
// this often.
constexpr int32_t kVerifyPollMs = 200;
// The companion app asks for a report every few seconds while it is connected. A request this
// recent means a phone nearby can show the collar's position, even with the base out of reach.
constexpr uint32_t kAppNearbyMs = 60 * 1000;

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
/// advertising stops altogether, unless a companion app is linked: then the collar keeps
/// advertising its ordinary name, slowly, so that the owner's phone can still connect and silence
/// the beacon. Meshtastic's normal advertising comes back when the beacon ends.
/// Needs Bluetooth enabled in the collar's Meshtastic settings: with it disabled, Meshtastic frees
/// the Bluetooth memory at boot and it cannot be started again without a reboot.
class BeaconAdvertiser
{
  public:
    /// Call every second or so with the wanted state (nullptr name = beacon off, "" = its quiet
    /// half). connectable: stay reachable for the companion app during the quiet half.
    void update(const char *name, uint32_t nowMs, bool connectable = false)
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
        // A phone is connected (isConnected() says so only once it has paired): leave Bluetooth alone.
        BLEServer *server = BLEDevice::getServer();
        if (nimbleBluetooth->isConnected() || (server && server->getConnectedCount() > 0))
            return;
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
            const Mode want = connectable ? Mode::Quiet : Mode::Off;
            if (mode_ != want) {
                BLEAdvertising *adv = BLEDevice::getAdvertising();
                adv->stop();
                if (connectable) {
                    // What Meshtastic itself advertises (its service, and the name in the scan
                    // response), only less often.
                    adv->reset();
                    adv->addServiceUUID(MESH_SERVICE_UUID);
                    BLEAdvertisementData scan;
                    scan.setName(getDeviceName());
                    adv->setScanResponseData(scan);
                    adv->setMinInterval(kQuietAdvInterval);
                    adv->setMaxInterval(kQuietAdvInterval);
                    if (!adv->start(0))
                        LOG_ERROR("GeoCollar: quiet advertising failed");
                }
                mode_ = want;
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
    enum class Mode : uint8_t { Normal, Beacon, Off, Quiet };
    Mode mode_ = Mode::Normal;
    bool warned_ = false;
    char name_[32] = {};
    uint32_t appliedMs_ = 0;
};

/// Slower Bluetooth advertising while the lost beacon is off. Meshtastic advertises every 30 to
/// 60 ms (its BLE library's default), which keeps the radio busy all day for a phone that is
/// rarely near. Every GEOCOLLAR_BLE_ADV_MS is still quick to find for the Meshtastic app (and the
/// GeoCollar app), and it is what the beacon itself uses. Nothing is changed while a phone is
/// connected. Meshtastic starts its fast advertising again after every disconnect and when the
/// beacon ends; this notices within kSlowAdvRefreshMs and slows it down again.
class SlowAdvertiser
{
  public:
    /// Call about once a second while the lost beacon is off.
    void update(uint32_t nowMs)
    {
#if GEOCOLLAR_HAS_BLE
        if (GEOCOLLAR_BLE_ADV_MS == 0)
            return;
        // Not called for a while (the beacon was on, with its own advertising): look afresh.
        if (nowMs - calledMs_ > 5000)
            applied_ = false;
        calledMs_ = nowMs;
        if (!nimbleBluetooth || !nimbleBluetooth->isActive()) {
            applied_ = false;
            return;
        }
        BLEServer *server = BLEDevice::getServer();
        if (nimbleBluetooth->isConnected() || (server && server->getConnectedCount() > 0)) {
            applied_ = false; // a phone is connected: leave Bluetooth alone
            return;
        }
        BLEAdvertising *adv = BLEDevice::getAdvertising();
        if (!adv->isAdvertising()) {
            applied_ = false; // Meshtastic is about to start it again (after a disconnect)
            return;
        }
        if (applied_ && nowMs - appliedMs_ < kSlowAdvRefreshMs)
            return;
        // Same advertisement, only less often: stop and start keep Meshtastic's data.
        const uint16_t units = (uint32_t)GEOCOLLAR_BLE_ADV_MS * 1000 / 625; // 0.625 ms units
        adv->stop();
        adv->setMinInterval(units);
        adv->setMaxInterval(units);
        if (!adv->start(0)) {
            LOG_ERROR("GeoCollar: slow advertising failed"); // Meshtastic's own may be restarting
            applied_ = false;
            return;
        }
        if (!applied_)
            LOG_DEBUG("GeoCollar: Bluetooth advertising every %u ms", (unsigned)GEOCOLLAR_BLE_ADV_MS);
        applied_ = true;
        appliedMs_ = nowMs;
#else
        (void)nowMs;
#endif
    }

  private:
    bool applied_ = false;
    uint32_t appliedMs_ = 0;
    uint32_t calledMs_ = 0;
};

/// Listens for the home anchor's Bluetooth: a beacon by its address, or the base radio by the end
/// of its Meshtastic Bluetooth name ("Meshtastic_1a2b", where 1a2b ends its node ID). Uses
/// NimBLE's scanning directly, which the Bluetooth controller runs by itself between windows:
/// the scan callback runs in NimBLE's task and only keeps the strongest reading for poll().
/// Bluetooth must be on in the collar's Meshtastic settings, as for the lost beacon.
class AnchorScanner
{
  public:
    /// Call about once a second. Starts, adjusts or stops the scan, and returns the strongest
    /// reading of the anchor since the last call (INT_MIN: none).
    int poll(const HomeAnchor &anchor, uint32_t baseNode, bool home)
    {
#if GEOCOLLAR_HAS_BLE
        const bool want = anchor.kind != AnchorKind::None && nimbleBluetooth && nimbleBluetooth->isActive() &&
                          (anchor.kind != AnchorKind::BaseRadio || baseNode != 0);
        char suffix[6];
        snprintf(suffix, sizeof(suffix), "_%04x", (unsigned)(baseNode & 0xFFFF));
        const bool same = anchor.kind == kind_ && memcmp(anchor.address, address_, sizeof(address_)) == 0 &&
                          memcmp(suffix, suffix_, sizeof(suffix_)) == 0;
        const uint16_t itvl = home ? kScanHomeItvl : kScanAwayItvl;
        if (!want || !same || itvl != itvl_) {
            stop();
            if (!want)
                return INT_MIN;
            // Stopped, so the callback is not reading these while they change.
            kind_ = anchor.kind;
            memcpy(address_, anchor.address, sizeof(address_));
            memcpy(suffix_, suffix, sizeof(suffix_));
            matched_ = false;
            itvl_ = itvl;
        }
        if (!ble_gap_disc_active()) {
            ble_gap_disc_params p = {};
            p.itvl = itvl_;
            p.window = kScanWindow;
            p.filter_policy = 0; // everyone: the callback picks the anchor
            p.limited = 0;
            p.passive = kind_ == AnchorKind::Beacon; // the base radio's name is in its scan response
            p.filter_duplicates = 0;                  // every reading, for its signal strength
            const int rc = ble_gap_disc(BLE_OWN_ADDR_PUBLIC, BLE_HS_FOREVER, &p, onGapEvent, this);
            if (rc != 0 && !warned_) {
                LOG_WARN("GeoCollar: cannot listen for the home anchor (NimBLE %d), trying again", rc);
                warned_ = true;
            } else if (rc == 0) {
                warned_ = false;
            }
        }
        return best_.exchange(INT_MIN);
#else
        (void)anchor;
        (void)baseNode;
        (void)home;
        return INT_MIN;
#endif
    }

  private:
#if GEOCOLLAR_HAS_BLE
    void stop()
    {
        if (ble_gap_disc_active())
            ble_gap_disc_cancel();
        best_.store(INT_MIN);
    }

    static int onGapEvent(struct ble_gap_event *event, void *arg)
    {
        if (event->type != BLE_GAP_EVENT_DISC)
            return 0;
        AnchorScanner *self = static_cast<AnchorScanner *>(arg);
        const auto &d = event->disc;
        bool match = false;
        if (self->kind_ == AnchorKind::Beacon) {
            match = true; // NimBLE keeps the address least significant byte first
            for (int i = 0; i < 6; i++)
                match = match && d.addr.val[5 - i] == self->address_[i];
        } else if (self->matched_ && memcmp(d.addr.val, self->matchedAddr_, 6) == 0) {
            match = true; // the base radio, found by its name before: its advertisements count too
        } else {
            ble_hs_adv_fields f;
            if (ble_hs_adv_parse_fields(&f, d.data, d.length_data) == 0 && f.name && f.name_len >= 5 &&
                memcmp(f.name + f.name_len - 5, self->suffix_, 5) == 0) {
                memcpy(self->matchedAddr_, d.addr.val, 6);
                self->matched_ = true;
                match = true;
            }
        }
        if (match) {
            int prev = self->best_.load();
            while (d.rssi > prev && !self->best_.compare_exchange_weak(prev, d.rssi)) {
            }
        }
        return 0;
    }

    AnchorKind kind_ = AnchorKind::None;
    uint8_t address_[6] = {};
    char suffix_[6] = {};
    uint8_t matchedAddr_[6] = {};
    volatile bool matched_ = false;
    uint16_t itvl_ = 0;
    bool warned_ = false;
    std::atomic<int> best_{INT_MIN};
#endif
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
        loadHome();
        applyPowerSettings();
        LOG_INFO("GeoCollar: ready, fence v%u, %u zones", engine_.fences().version, engine_.fences().zoneCount);
    }

  protected:
    /// GeoCollar's own port, and anything else from the base (acknowledgements of the collar's
    /// reports, its broadcasts), which only says that the base is in reach (see BaseLink).
    bool wantPacket(const meshtastic_MeshPacket *p) override
    {
        return SinglePortModule::wantPacket(p) || (baseNode_ && p->from == baseNode_ && !isFromUs(p));
    }

    // Only accept GeoCollar packets that are PKI direct messages from an authorized admin key,
    // i.e. the owner's base station. Everything else on our port is ignored.
    ProcessMessage handleReceived(const meshtastic_MeshPacket &mp) override
    {
        if (baseNode_ && mp.from == baseNode_ && !isFromUs(&mp))
            link_.onHeard();
        if (mp.decoded.portnum != static_cast<meshtastic_PortNum>(proto::kPortNum))
            return ProcessMessage::CONTINUE; // not ours: Meshtastic's own modules handle it as usual
        if (isFromUs(&mp)) {
            // From a client plugged into this collar itself, which for a collar means the
            // companion app over Bluetooth. Being connected proves nothing: handleApp() checks
            // the app's key.
            if (!isToUs(&mp))
                return ProcessMessage::CONTINUE;
            handleApp(mp);
            return ProcessMessage::STOP;
        }
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
        link_.onHeard();

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
                        verifying_ = false;
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
                if (c.hasAnchor)
                    setAnchor(c.anchor);
                if (c.hasGpsInside && (setGpsInsideSecs(engine_.settings(), c.gpsInsideS) ||
                                       c.gpsSameEverywhere != gpsSameEverywhere_)) {
                    gpsSameEverywhere_ = c.gpsSameEverywhere;
                    save = true;
                    LOG_INFO("GeoCollar: GPS inside the fences every %u s%s, %s", (unsigned)c.gpsInsideS,
                             c.gpsInsideS ? "" : " (always on)",
                             gpsSameEverywhere_ ? "everywhere inside" : "within 35 m of the bands");
                }
                LOG_INFO("GeoCollar: report every %u s", (unsigned)(reportS_ ? reportS_ : GEOCOLLAR_STATUS_INTERVAL_MS / 1000));
            }
        } else if (type == proto::MsgType::AppKeys) {
            uint32_t seq = 0;
            if (!proto::decodeAppKeys(buf, len, scratchKeys_, seq)) {
                LOG_WARN("GeoCollar: malformed app key list");
            } else if (seq <= lastAppKeysSeq_) {
                // A recording of an older list could put a removed phone back.
                LOG_WARN("GeoCollar: refusing app key list %u, not newer than %u (a replay?)", (unsigned)seq,
                         (unsigned)lastAppKeysSeq_);
            } else {
                appKeys_ = scratchKeys_;
                lastAppKeysSeq_ = seq;
                app_.keysChanged(appKeys_); // a phone that was just removed is cut off now
                save = true;
                LOG_INFO("GeoCollar: %u companion app(s) authorized", (unsigned)appKeys_.count);
                sendAppKeysAck();
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
            savePowerSettings();
        }
        watchForFixes();

        checkPosition(); // a new fix may start a correction, or stop one that is playing
        if (!tx_.busy()) {
            // The rest waits while frames are going out, so that they follow each other closely.
            repeatPulse();
            runBeacon();
            const uint32_t now = millis();
            watchGps(now);
            runHome(now);
            runLink(now);
            applyGpsPacing();
            report(now);
        }
        // Send the next frame, or the first one of a correction started above.
        int32_t next = tx_.busy() ? tx_.poll() : 1000;
        if (verifying_) {
            // Back when the wait for the second look is over, and then until the GPS has a reading.
            const int32_t wait = (int32_t)(verifyAtMs_ - millis());
            const int32_t soon = wait > 0 ? wait : kVerifyPollMs;
            if (soon < next)
                next = soon;
        }
        return next;
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
        if (status->getHasLock() && takeReading())
            setIntervalFromNow(0); // check the fences now, not at the next once-a-second pass
        return 0;
    }

    /// Take the GPS's newest reading for runOnce(), if it is one the fences have not had yet.
    bool takeReading()
    {
        if (!gps || config.position.fixed_position)
            return false;
        const meshtastic_Position &p = gps->p;
        if (p.timestamp == 0 || p.timestamp == lastFixTimestamp_ || (p.latitude_i == 0 && p.longitude_i == 0))
            return false; // not a new fix (the GPS also publishes when it only has the time)
        lastFixTimestamp_ = p.timestamp;
        pendingFix_ = Fix{p.latitude_i, p.longitude_i, clampU16(p.PDOP), clampU8(p.sats_in_view), millis()};
        fixPending_ = true;
        return true;
    }
#endif

    /// The second look: the fence engine holds a correction back until a reading taken at least a
    /// second after the first still has the dog in the band (see FenceEngine::onFix). Returns
    /// true once there is such a reading to check, stamped with the time it is looked at.
    ///
    /// Near a line the GPS is on all the time and has a new reading every second, so the wait
    /// ends with one about a second old at most. A reading that comes during the wait is kept
    /// until the wait is over. If the GPS has gone to sleep (it does between fixes more than 10 s
    /// apart, so only when the dog got into a band faster than the pacing allows for, or with
    /// pacing off), it is woken now; Meshtastic's GPS thread then needs up to 5 s to read it.
    bool secondLook(uint32_t now)
    {
        if ((int32_t)(now - verifyAtMs_) < 0)
            return false; // the second is not over yet
        if (now - verifyAtMs_ > kVerifyStaleMs) {
            verifying_ = false; // no reading in all that time: the next one starts the wait again
            return fixPending_;
        }
#if GEOCOLLAR_HAS_GPS
        if (gps && gps->hasLock()) {
            // The newest reading, whether Meshtastic has announced it yet or not: with fixes more
            // than 10 s apart it keeps a new lock to itself for up to 20 s.
            takeReading();
        } else if (gps && !verifyWoke_ && !fixPending_ &&
                   config.position.gps_mode == meshtastic_Config_PositionConfig_GpsMode_ENABLED &&
                   !config.position.fixed_position) {
            verifyWoke_ = true;
            lastGpsKickMs_ = now;
            gps->up();
        }
#endif
        if (fixPending_)
            pendingFix_.nowMs = now;
        return fixPending_;
    }

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
        if (verifying_ ? !secondLook(millis()) : !fixPending_)
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
        if (d.verify && !(verifying_ && d.verifyAtMs == verifyAtMs_))
            verifyWoke_ = false; // a new wait: the GPS may be woken for it once
        verifying_ = d.verify;
        verifyAtMs_ = d.verifyAtMs;

        lastClearance_ = d.clearanceM; // 0 for a fix too poor to use, which also makes the GPS fix often
        if (!d.fixRejected)
            beacon_.onFix(engine_.state() != CollarState::NoFence && d.clearanceM < 0, fix.nowMs);
        if (!d.fixRejected) // clear of every band, or not: home mode starts only from a clear fix
            home_.onFix(engine_.state() == CollarState::Inside, fix.nowMs);
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
    /// long. With fences loaded the search is restarted every minute instead, which keeps the GPS
    /// powered while the dog is indoors. (On the Wireless Tracker Meshtastic powers the GPS down
    /// between fixes more than 10 s apart, by switching off Vext while the screen is dark too.)
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
                   !config.position.fixed_position && !home_.home() && !gpsOutOfReach_ &&
                   now - lastGpsKickMs_ >= kGpsKickMs) {
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
        const uint32_t usual = gpsIntervalSecs(engine_.settings(), engine_.state(), lastClearance_,
                                               alertWidthM(engine_.fences()), beacon_.active(), true, gpsSameEverywhere_);
        // A lost dog's position is only worth fixing as often as something can pass it on: the
        // base (directly or through the mesh) or a phone with the companion app nearby.
        const bool relayed = link_.reachable() || (lastAppRequestMs_ && millis() - lastAppRequestMs_ < kAppNearbyMs);
        const uint32_t want = relayed ? usual
                                      : gpsIntervalSecs(engine_.settings(), engine_.state(), lastClearance_,
                                                        alertWidthM(engine_.fences()), beacon_.active(), false,
                                                        gpsSameEverywhere_);
        const bool outOfReach = want != usual;
        if (outOfReach != gpsOutOfReach_)
            LOG_INFO("GeoCollar: %s", outOfReach ? "lost and nothing can pass the position on: a fix every 5 minutes"
                                                 : "the position can be passed on: lost-mode fixes as usual");
        // Meshtastic adds a wait after each lock to the interval it is given (gpsSettingSecs), so
        // it is given less, to make the fixes come every `want` seconds.
        // Home: as slow as Meshtastic goes, and powered down below.
        const uint32_t target = home_.home() ? kHomeGpsSecs : want ? gpsSettingSecs(want) : ownerGpsSecs_;
        if (current != target) {
            LOG_DEBUG("GeoCollar: GPS interval %us -> %us", (unsigned)current, (unsigned)target);
            current = target;
        }
        lastPacedSecs_ = current;
#if GEOCOLLAR_HAS_GPS
        // Back in reach (or a phone came): the GPS may be asleep for minutes on the slow interval,
        // which Meshtastic fixed when it put it to sleep, so wake it for a fix now.
        if (gpsOutOfReach_ && !outOfReach && !home_.home() && gps &&
            config.position.gps_mode == meshtastic_Config_PositionConfig_GpsMode_ENABLED && !config.position.fixed_position)
            gps->up();
#endif
        gpsOutOfReach_ = outOfReach;
#if GEOCOLLAR_HAS_GPS
        // Home mode switches the GPS off and on here, after the interval is set: Meshtastic reads
        // it to decide how long the GPS sleeps, and that it reports a fix at once when it wakes.
        if (home_.home() != gpsHome_) {
            gpsHome_ = home_.home();
            if (gps && config.position.gps_mode == meshtastic_Config_PositionConfig_GpsMode_ENABLED &&
                !config.position.fixed_position) {
                if (gpsHome_)
                    gps->down(); // asleep, with Vext off while the screen is dark, for up to an hour
                else
                    gps->up(); // a fix as soon as it has one
            }
        }
#endif
    }

    /// Whether the base is answering the collar's reports (see BaseLink). applyGpsPacing() acts on it.
    void runLink(uint32_t now)
    {
        if (link_.tick(now))
            LOG_INFO("GeoCollar: base %s", link_.reachable() ? "in reach again" : "out of reach: no answer to the last reports");
    }

    /// Home mode: the GPS off while the collar is close to the anchor (see HomeWatch).
    void runHome(uint32_t now)
    {
        if (engine_.fences().version != homeFenceVersion_) {
            homeFenceVersion_ = engine_.fences().version;
            home_.reset(); // new fences: a fix must show the dog clear of them first
        }
        const int rssi = scanner_.poll(anchor_, baseNode_, home_.home());
        if (rssi != INT_MIN)
            home_.onSighting(rssi, now);
        const bool allowed = anchor_.kind != AnchorKind::None && engine_.state() == CollarState::Inside && !beacon_.active();
        if (!home_.tick(now, allowed))
            return;
        if (home_.home()) {
            LOG_INFO("GeoCollar: home, near the anchor: GPS off");
        } else {
            LOG_INFO("GeoCollar: away from the anchor: GPS on");
            lastClearance_ = 0; // fix often until a fix says where the dog is
        }
        sendStatus(statusType());
    }

    void setAnchor(const HomeAnchor &a)
    {
        if (a.kind == anchor_.kind && memcmp(a.address, anchor_.address, sizeof(a.address)) == 0 &&
            a.rssiDbm == anchor_.rssiDbm)
            return;
        anchor_ = a;
        home_.setThreshold(a.rssiDbm);
        home_.reset();
        saveHome();
        LOG_INFO("GeoCollar: home anchor %s, %d dBm",
                 a.kind == AnchorKind::Beacon ? "beacon" : a.kind == AnchorKind::BaseRadio ? "base radio" : "off",
                 (int)a.rssiDbm);
    }

    void saveHome()
    {
        {
            concurrency::LockGuard g(spiLock);
            FSCom.mkdir("/prefs");
        }
        SafeFile f(kHomeFile, true);
        const uint8_t kind = static_cast<uint8_t>(anchor_.kind);
        const int8_t rssi = anchor_.rssiDbm;
        f.write((const uint8_t *)&kHomeMagic, sizeof(kHomeMagic));
        f.write(&kHomeFormat, 1);
        f.write(&kind, 1);
        f.write(anchor_.address, sizeof(anchor_.address));
        f.write((const uint8_t *)&rssi, 1);
        if (!f.close())
            LOG_ERROR("GeoCollar: failed to save %s", kHomeFile);
    }

    void loadHome()
    {
        concurrency::LockGuard g(spiLock);
        if (!FSCom.exists(kHomeFile))
            return;
        auto f = FSCom.open(kHomeFile, FILE_O_READ);
        if (!f)
            return;
        uint32_t magic = 0;
        uint8_t format = 0, kind = 0;
        HomeAnchor a;
        int8_t rssi = 0;
        const bool ok = f.read((uint8_t *)&magic, sizeof(magic)) == sizeof(magic) && magic == kHomeMagic &&
                        f.read(&format, 1) == 1 && format == kHomeFormat && f.read(&kind, 1) == 1 &&
                        kind <= static_cast<uint8_t>(AnchorKind::BaseRadio) &&
                        f.read(a.address, sizeof(a.address)) == sizeof(a.address) && f.read((uint8_t *)&rssi, 1) == 1 &&
                        rssi >= kMinAnchorRssi && rssi <= kMaxAnchorRssi;
        f.close();
        if (!ok)
            return; // no anchor: home mode stays off
        a.kind = static_cast<AnchorKind>(kind);
        a.rssiDbm = rssi;
        anchor_ = a;
        home_.setThreshold(rssi);
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
            slowAdv_.update(now);
            return;
        }
        char name[30];
        if (beacon_.bleOn(now))
            beaconName(owner_.dog[0] ? owner_.dog : "Dog", owner_.phone, name, sizeof(name));
        else
            name[0] = 0;
        ble_.update(name, now, appKeys_.count > 0);
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
            verifying_ = false;
            break;
        case proto::CommandAction::ClearLost:
            engine_.clearLost();
            verifying_ = false;
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

    /// Meshtastic settings that cost battery and do nothing for a collar, put right in RAM. Runs
    /// from the constructor: before Meshtastic starts Wi-Fi, sets up its power states (screen
    /// timeout, light sleep) and picks the CPU speed, so they all see these values. The changed
    /// settings are saved from the first runOnce() (savePowerSettings).
    void applyPowerSettings()
    {
#if GEOCOLLAR_WIFI_OFF
        if (config.network.wifi_enabled) {
            // Bluetooth was already left out of this boot if a network was set (Meshtastic
            // decides that before modules start), so restart once the setting is saved.
            powerRestart_ = config.network.wifi_ssid[0] != 0;
            config.network.wifi_enabled = false;
            powerSave_ = true;
            LOG_WARN("GeoCollar: Wi-Fi turned off, to save battery and keep Bluetooth for the lost beacon");
        }
#endif
        // Meshtastic's power saving puts an ESP32 into light sleep for minutes at a time (ls_secs,
        // 5 minutes by default) with Bluetooth off: no GPS readings, no fence and no corrections
        // in that time. On a collar that costs more than it saves.
        if (config.power.is_power_saving) {
            config.power.is_power_saving = false;
            powerSave_ = true;
            LOG_WARN("GeoCollar: Meshtastic power saving turned off: its light sleep would leave the fence unwatched");
        }
#if GEOCOLLAR_NO_RELAY
        if (config.device.role == meshtastic_Config_DeviceConfig_Role_CLIENT) {
            // Still sends and receives its own messages; other nodes still relay for it.
            config.device.role = meshtastic_Config_DeviceConfig_Role_CLIENT_MUTE;
            powerSave_ = true;
            LOG_INFO("GeoCollar: role Client Mute, so the collar does not relay other nodes' packets");
        }
#endif
        // Set in RAM only: Meshtastic writes it with the rest of its settings the next time it
        // saves them, which changes nothing, since it is replaced here only while it is a default.
        if (GEOCOLLAR_SCREEN_ON_SECS &&
            (config.display.screen_on_secs == 0 || config.display.screen_on_secs == kScreenOnSecs))
            config.display.screen_on_secs = GEOCOLLAR_SCREEN_ON_SECS;
    }

    /// Save what applyPowerSettings() changed, so a restart does not start Wi-Fi again.
    void savePowerSettings()
    {
        if (!powerSave_ || !nodeDB)
            return;
        powerSave_ = false;
        nodeDB->saveToDisk(SEGMENT_CONFIG);
        if (powerRestart_) {
            LOG_WARN("GeoCollar: restarting in 5 s, to start Bluetooth without Wi-Fi");
            rebootAtMsec = millis() + 5000;
        }
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

    /// The companion app, connected to this collar as a local client. It may be anyone who
    /// paired over Bluetooth, so nothing is done for it without its key's signature.
    void handleApp(const meshtastic_MeshPacket &mp)
    {
        if (mp.which_payload_variant != meshtastic_MeshPacket_decoded_tag)
            return;
        uint8_t nonce[proto::kAppNonceLen];
        esp_fill_random(nonce, sizeof(nonce));
        uint8_t reply[proto::kAppStatusLength];
        size_t n = 0;
        proto::AppRequest req;
        switch (app_.handle(mp.decoded.payload.bytes, mp.decoded.payload.size, appKeys_, nonce, reply, sizeof(reply), n, req)) {
        case proto::AppLink::Outcome::Ignore:
            return;
        case proto::AppLink::Outcome::Reply:
            break;
        case proto::AppLink::Outcome::Request: {
            lastAppRequestMs_ = millis() | 1; // a phone is connected, and can show where the dog is
            proto::AppResult result = proto::AppResult::Done;
            if (!proto::appActionAllowed(req.action)) {
                result = proto::AppResult::NotAllowed;
                LOG_WARN("GeoCollar: the app asked for action %u, which apps may not", (unsigned)req.action);
            } else if (req.action != proto::CommandAction::RequestStatus) {
                // The same path as a command from the base, which also tells the base what changed.
                // A plain request for a report stays off the mesh: the app asks every few seconds.
                LOG_INFO("GeoCollar: app command %u", (unsigned)req.action);
                proto::Command cmd{req.action, req.intensity, req.durationMs, 0};
                handleCommand(cmd);
                if (req.action == proto::CommandAction::CorrectionsOff || req.action == proto::CommandAction::CorrectionsOn)
                    saveState();
                if (tx_.busy())
                    setIntervalFromNow(0); // send the first frame now
            }
            n = app_.status(reply, sizeof(reply), req, result, currentStatus(), fixAgeSecs());
            break;
        }
        }
        if (!n)
            return;
        meshtastic_MeshPacket *p = allocDataPacket();
        if (!p)
            return;
        p->to = nodeDB->getNodeNum(); // from this collar to itself: handed to the phone, never sent on the air
        p->want_ack = false;
        memcpy(p->decoded.payload.bytes, reply, n);
        p->decoded.payload.size = n;
        service->sendToPhone(p);
    }

    /// Seconds since the position in the report was taken; kAppNoFixAge if there has been none.
    uint16_t fixAgeSecs() const
    {
        if (lastFixTimestamp_ == 0)
            return proto::kAppNoFixAge;
        const uint32_t age = (millis() - lastFix_.nowMs) / 1000;
        return age >= proto::kAppNoFixAge ? proto::kAppNoFixAge - 1 : static_cast<uint16_t>(age);
    }

    /// Tell the base which list of app keys is in force.
    void sendAppKeysAck()
    {
        if (!baseNode_)
            return;
        meshtastic_MeshPacket *p = allocDataPacket();
        if (!p)
            return;
        p->to = baseNode_;
        p->want_ack = true;
        p->decoded.payload.size =
            proto::encodeAppKeysAck(p->decoded.payload.bytes, sizeof(p->decoded.payload.bytes), lastAppKeysSeq_, appKeys_.count);
        service->sendToMesh(p, RX_SRC_LOCAL, false);
        link_.onSent(millis());
    }

    proto::StatusReport currentStatus() const
    {
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
                  proto::kFlagNumbered | (appKeys_.count ? proto::kFlagApps : 0) |
                  (home_.home() ? proto::kFlagHome : 0);
        return s;
    }

    void sendStatus(proto::MsgType type)
    {
        if (!baseNode_)
            return;
        const proto::StatusReport s = currentStatus();

        meshtastic_MeshPacket *p = allocDataPacket();
        if (!p)
            return;
        p->to = baseNode_; // a direct message: Meshtastic encrypts it with PKI when it knows the key
        p->want_ack = true;
        p->decoded.payload.size =
            proto::encodeStatus(p->decoded.payload.bytes, sizeof(p->decoded.payload.bytes), type, s);
        service->sendToMesh(p, RX_SRC_LOCAL, false);
        link_.onSent(millis());
        lastStatusMs_ = millis();
        bootReported_ = true;
        reportedNoFix_ = fixWatch_.noFix();
    }

    // Persistence: [magic u32][format u16][base node u32][fence version u16][blob len u16][blob][Settings]
    // [OwnerInfo][command seq u32][report interval s u16][owner seq u32][config seq u32]
    // [owner's GPS interval s u32][that value is known u8][app keys seq u32][AppKeySet]
    // [GPS interval everywhere inside u8]
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
        f.write((const uint8_t *)&lastAppKeysSeq_, sizeof(lastAppKeysSeq_));
        f.write((const uint8_t *)&appKeys_, sizeof(appKeys_));
        const uint8_t everywhere = gpsSameEverywhere_;
        f.write(&everywhere, sizeof(everywhere));
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
        uint32_t seq = 0, ownerSeq = 0, configSeq = 0, gpsSecs = 0, appSeq = 0;
        proto::AppKeySet appKeys;
        uint16_t reportS = 0;
        uint8_t gpsKnown = 0, everywhere = 0;
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
                                  f.read(&gpsKnown, sizeof(gpsKnown)) == sizeof(gpsKnown))) &&
                  (format < 9 || (f.read((uint8_t *)&appSeq, sizeof(appSeq)) == sizeof(appSeq) &&
                                  f.read((uint8_t *)&appKeys, sizeof(appKeys)) == sizeof(appKeys) &&
                                  appKeys.count <= proto::kMaxAppKeys)) &&
                  (format < 10 || f.read(&everywhere, sizeof(everywhere)) == sizeof(everywhere));
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
            lastAppKeysSeq_ = appSeq;
            appKeys_ = appKeys;
            gpsSameEverywhere_ = everywhere == 1;
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
    proto::AppKeySet appKeys_;     // the phones the base has authorized (saved)
    proto::AppKeySet scratchKeys_; // a list being decoded
    proto::AppLink app_;           // the session with the phone that is connected, if any
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
    bool verifying_ = false;   // a correction is held back for its second look (see secondLook)
    uint32_t verifyAtMs_ = 0;  // when the wait for it is over
    bool verifyWoke_ = false;  // the GPS has been woken for it
    FixWatch fixWatch_;         // no current fix: none since boot, or the last one is overdue
    bool loggedNoFix_ = true;
    bool reportedNoFix_ = true; // what the last report told the base about that
    uint32_t lastNoFixReportMs_ = 0;
    uint32_t lastGpsKickMs_ = 0;
    bool kickWaited_ = false; // the last restart was put off because the GPS had a lock
    float lastClearance_ = 0;
    SlowAdvertiser slowAdv_;
    AnchorScanner scanner_;
    HomeAnchor anchor_;            // from the base (kHomeFile); kind None: home mode off
    HomeWatch home_;
    uint16_t homeFenceVersion_ = 0; // the fences home_ last saw
    bool gpsHome_ = false;          // applyGpsPacing() has powered the GPS down for home mode
    BaseLink link_;                 // whether the base answers the collar's reports
    bool gpsOutOfReach_ = false;    // applyGpsPacing() has slowed a lost dog's GPS: nothing can pass the position on
    uint32_t lastAppRequestMs_ = 0; // the companion app's latest request (0: none since boot)
    bool gpsSameEverywhere_ = false; // the owner's GPS interval wherever the dog is inside (saved)
    bool powerSave_ = false;    // applyPowerSettings() changed a setting that has to be saved
    bool powerRestart_ = false; // and Bluetooth needs a restart to come back
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
    uint32_t lastAppKeysSeq_ = 0; // and for the list of app keys
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
