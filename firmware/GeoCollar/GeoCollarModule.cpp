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
#include "MeshService.h"
#include "NodeDB.h"
#include "PowerStatus.h"
#include "SPILock.h"
#include "SafeFile.h"
#include "SinglePortModule.h"
#include "concurrency/LockGuard.h"
#include "concurrency/OSThread.h"
#include "mesh/AdminKeys.h"

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
constexpr uint16_t kStateFormat = 7; // 2: GPS pacing; 3: per-zone bands; 4: pulse intervals; 5: owner; 6: command seq;
                                     // 7: report interval
constexpr uint32_t kBeaconRefreshMs = 10 * 1000; // re-apply the beacon advertisement this often
// Until a base adopts the collar it says hello on the primary channel, so the base app can list it:
// every minute for the first 15 minutes after boot, then every 15 minutes.
constexpr uint32_t kHelloFastMs = 60 * 1000, kHelloSlowMs = 15 * 60 * 1000, kHelloFastForMs = 15 * 60 * 1000;

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

    /// Start (or replace) a correction. durationMs is clamped to the firmware maximum.
    void start(CollarAction action, uint8_t intensity, uint32_t durationMs)
    {
        if (durationMs > GEOCOLLAR_MAX_CORRECTION_MS)
            durationMs = GEOCOLLAR_MAX_CORRECTION_MS;
        action_ = action;
        intensity_ = intensity;
        const uint32_t now = millis();
        activeUntil_ = now + durationMs;
        stopUntil_ = activeUntil_ + GEOCOLLAR_STOP_MS;
        busy_ = true;
    }

    /// Stop now: send only the stop frame (vibrate at 0) for a short while.
    void stop()
    {
        const uint32_t now = millis();
        activeUntil_ = now;
        stopUntil_ = now + GEOCOLLAR_STOP_MS;
        busy_ = true;
    }

    bool busy() const { return busy_; }

    /// Send the next frame if the previous one finished. Returns ms until the next poll is due.
    int32_t poll()
    {
        if (!busy_ || !ready_)
            return 1000;
        if (!rmtTransmitCompleted(GEOCOLLAR_TX_PIN))
            return 5;

        const uint32_t now = millis();
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
        caixianlin::buildFrame(pulses, GEOCOLLAR_TRANSMITTER_ID, GEOCOLLAR_CHANNEL, a, level);
        for (size_t i = 0; i < caixianlin::kFrameLength; i++) {
            symbols_[i].level0 = 1;
            symbols_[i].duration0 = pulses[i].highUs;
            symbols_[i].level1 = 0;
            symbols_[i].duration1 = pulses[i].lowUs;
        }
        rmtWriteAsync(GEOCOLLAR_TX_PIN, symbols_, caixianlin::kFrameLength);
        return caixianlin::kFrameUs / 1000 + 1;
    }

  private:
    bool ready_ = false;
    bool busy_ = false;
    CollarAction action_ = CollarAction::Beep;
    uint8_t intensity_ = 0;
    uint32_t activeUntil_ = 0;
    uint32_t stopUntil_ = 0;
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
                BLEAdvertising *adv = BLEDevice::getAdvertising();
                adv->stop();
                adv->setMinInterval(0x20); // the library defaults Meshtastic advertises with
                adv->setMaxInterval(0x40);
                nimbleBluetooth->startAdvertising();
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
            LOG_ERROR("GeoCollar: lost beacon advertising failed");
        else if (mode_ != Mode::Beacon)
            LOG_INFO("GeoCollar: lost beacon advertising \"%s\"", name);
        strncpy(name_, name, sizeof(name_) - 1);
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
        baseNode_ = mp.from;

        const uint8_t *buf = mp.decoded.payload.bytes;
        const size_t len = mp.decoded.payload.size;
        proto::MsgType type;
        if (!proto::messageType(buf, len, type))
            return ProcessMessage::STOP;

        if (type == proto::MsgType::FenceChunk) {
            proto::FenceChunk chunk;
            if (proto::decodeFenceChunk(buf, len, chunk) && assembler_.add(chunk)) {
                FenceSet fences;
                if (!proto::fenceVersionAcceptable(assembler_.version(), engine_.fences().version)) {
                    LOG_WARN("GeoCollar: refusing fence v%u, older than v%u (a replay?)", assembler_.version(),
                             engine_.fences().version);
                } else if (proto::decodeFenceSet(assembler_.data(), assembler_.length(), assembler_.version(), fences)) {
                    engine_.setFences(fences);
                    beacon_.reset();
                    saveState();
                    LOG_INFO("GeoCollar: fence v%u loaded, %u zones", fences.version, fences.zoneCount);
                    sendStatus(proto::MsgType::Status);
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
                handleCommand(cmd); // every command path below saves state or sends status
                saveState();
            }
        } else if (type == proto::MsgType::Owner) {
            proto::OwnerInfo o;
            if (proto::decodeOwner(buf, len, o)) {
                owner_ = o;
                saveState();
                LOG_INFO("GeoCollar: dog's name \"%s\", owner contact set", owner_.dog);
                applyMeshName();
                sendStatus(proto::MsgType::Status);
            }
        } else if (type == proto::MsgType::Config) {
            proto::CollarConfig c;
            if (proto::decodeConfig(buf, len, c)) {
                reportS_ = c.reportS;
                saveState();
                LOG_INFO("GeoCollar: report every %u s", (unsigned)(reportS_ ? reportS_ : GEOCOLLAR_STATUS_INTERVAL_MS / 1000));
            }
        }
        return ProcessMessage::STOP;
    }

    int32_t runOnce() override
    {
        if (!named_) {
            named_ = true;
            nameFreshCollar();
        }
        const int32_t txDelay = tx_.poll();
        if (tx_.busy())
            return txDelay; // keep frames flowing; position checks resume once the collar is quiet

        checkPosition();
        repeatPulse();
        runBeacon();
        applyGpsPacing();

        const uint32_t now = millis();
        // The owner's report interval from the base; while lost, at least every 30 s.
        const uint32_t normal = reportS_ ? reportS_ * 1000u : GEOCOLLAR_STATUS_INTERVAL_MS;
        const uint32_t interval = engine_.state() == CollarState::Lost && normal > GEOCOLLAR_LOST_STATUS_INTERVAL_MS
                                      ? GEOCOLLAR_LOST_STATUS_INTERVAL_MS
                                      : normal;
        if (!baseNode_ && (lastHelloMs_ == 0 || now - lastHelloMs_ >= (now < kHelloFastForMs ? kHelloFastMs : kHelloSlowMs)))
            sendHello();
        if (baseNode_ && now - lastStatusMs_ >= interval)
            sendStatus(engine_.state() == CollarState::Lost ? proto::MsgType::Breach : proto::MsgType::Status);

        return 1000;
    }

  private:
    void checkPosition()
    {
        if (!nodeDB || !nodeDB->hasLocalPositionSinceBoot())
            return;
        if (localPosition.timestamp == lastFixTimestamp_)
            return;
        lastFixTimestamp_ = localPosition.timestamp;

        Fix fix{localPosition.latitude_i, localPosition.longitude_i, (uint16_t)localPosition.PDOP,
                (uint8_t)localPosition.sats_in_view, millis()};
        lastFix_ = fix;

        const bool hwDisabled = GEOCOLLAR_DISABLE_PIN >= 0 && digitalRead(GEOCOLLAR_DISABLE_PIN) == LOW;
        const bool savedEnable = engine_.settings().correctionsEnabled;
        if (hwDisabled)
            engine_.settings().correctionsEnabled = false;
        const Decision d = engine_.onFix(fix);
        engine_.settings().correctionsEnabled = savedEnable;

        lastClearance_ = d.clearanceM;
        if (!d.fixRejected)
            beacon_.onFix(engine_.state() != CollarState::NoFence && d.clearanceM < 0, millis());
        if (pulser_.onDecision(d, engine_.fences(), millis()))
            applyCorrection(d.correction, d.intensity);
        if (d.sendBreach)
            sendStatus(proto::MsgType::Breach);
        else if (d.sendStatus)
            sendStatus(proto::MsgType::Status);
    }

    // GPS pacing. Meshtastic's GPS thread reads config.position.gps_update_interval on every pass,
    // so changing it in RAM takes effect within about 5 s without saving the config. The owner's
    // own value is remembered and put back when there are no fences or pacing is off; if the owner
    // changes the interval over the mesh, that becomes the new value to put back.
    void applyGpsPacing()
    {
        uint32_t &current = config.position.gps_update_interval;
        if (current != lastPacedSecs_)
            ownerGpsSecs_ = current;
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
            sendStatus(engine_.state() == CollarState::Lost ? proto::MsgType::Breach : proto::MsgType::Status);
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
        const bool hwDisabled = GEOCOLLAR_DISABLE_PIN >= 0 && digitalRead(GEOCOLLAR_DISABLE_PIN) == LOW;
        const bool allowed = engine_.settings().correctionsEnabled && !hwDisabled && engine_.state() == CollarState::Warning;
        uint8_t intensity = 0;
        const Correction c = pulser_.poll(engine_.fences(), millis(), allowed, intensity);
        if (c != Correction::None)
            applyCorrection(c, intensity);
    }

    void applyCorrection(Correction c, uint8_t intensity)
    {
        const uint32_t ms = engine_.settings().correctionMs;
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

    void handleCommand(const proto::Command &cmd)
    {
        Settings &s = engine_.settings();
        switch (cmd.action) {
        case proto::CommandAction::Beep:
            tx_.start(CollarAction::Beep, 0, cmd.durationMs);
            break;
        case proto::CommandAction::Vibrate:
            tx_.start(CollarAction::Vibrate, cmd.intensity, cmd.durationMs);
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
            saveState();
            break;
        case proto::CommandAction::CorrectionsOn:
            s.correctionsEnabled = true;
            saveState();
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
                saveState();
                LOG_INFO("GeoCollar: shock on, level %u", s.shockLevel);
            }
            break;
        case proto::CommandAction::ShockOff:
            s.shockEnabled = false;
            saveState();
            LOG_INFO("GeoCollar: shock off");
            break;
        case proto::CommandAction::SilenceLost:
            beacon_.silence();
            ble_.update(nullptr, millis());
            break;
        }
        sendStatus(proto::MsgType::Status);
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
        s.flags = (engine_.settings().correctionsEnabled ? 1 : 0) | (engine_.settings().shockEnabled ? 2 : 0) |
                  (beacon_.active() ? 4 : 0) | (beacon_.silenced() ? 8 : 0);

        meshtastic_MeshPacket *p = allocDataPacket();
        if (!p)
            return;
        p->to = baseNode_; // a direct message: Meshtastic encrypts it with PKI when it knows the key
        p->want_ack = true;
        p->decoded.payload.size =
            proto::encodeStatus(p->decoded.payload.bytes, sizeof(p->decoded.payload.bytes), type, s);
        service->sendToMesh(p, RX_SRC_LOCAL, false);
        lastStatusMs_ = millis();
    }

    // Persistence: [magic u32][format u16][base node u32][fence version u16][blob len u16][blob][Settings]
    // [OwnerInfo][command seq u32][report interval s u16]
    void saveState()
    {
        uint8_t blob[proto::kMaxFenceBlob];
        const size_t blobLen = proto::encodeFenceSet(blob, sizeof(blob), engine_.fences());
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
        f.write(blob, blobLen);
        f.write((const uint8_t *)&engine_.settings(), sizeof(Settings));
        f.write((const uint8_t *)&owner_, sizeof(owner_));
        f.write((const uint8_t *)&lastCmdSeq_, sizeof(lastCmdSeq_));
        f.write((const uint8_t *)&reportS_, sizeof(reportS_));
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
        static uint8_t blob[proto::kMaxFenceBlob];
        Settings settings;
        proto::OwnerInfo owner;
        uint32_t seq = 0;
        uint16_t reportS = 0;
        bool ok = f.read((uint8_t *)&magic, sizeof(magic)) == sizeof(magic) && magic == kStateMagic &&
                  f.read((uint8_t *)&format, sizeof(format)) == sizeof(format) && format >= 4 && format <= kStateFormat &&
                  f.read((uint8_t *)&base, sizeof(base)) == sizeof(base) &&
                  f.read((uint8_t *)&version, sizeof(version)) == sizeof(version) &&
                  f.read((uint8_t *)&len16, sizeof(len16)) == sizeof(len16) && len16 <= sizeof(blob) &&
                  f.read(blob, len16) == len16 && f.read((uint8_t *)&settings, sizeof(settings)) == sizeof(settings) &&
                  (format < 5 || f.read((uint8_t *)&owner, sizeof(owner)) == sizeof(owner)) && // 4: no owner yet
                  (format < 6 || f.read((uint8_t *)&seq, sizeof(seq)) == sizeof(seq)) &&
                  (format < 7 || f.read((uint8_t *)&reportS, sizeof(reportS)) == sizeof(reportS));
        f.close();
        FenceSet fences;
        if (ok && proto::decodeFenceSet(blob, len16, version, fences)) {
            engine_.setFences(fences);
            engine_.settings() = settings;
            baseNode_ = base;
            owner.dog[sizeof(owner.dog) - 1] = owner.owner[sizeof(owner.owner) - 1] = owner.phone[sizeof(owner.phone) - 1] = 0;
            owner_ = owner;
            lastCmdSeq_ = seq;
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
    NodeNum baseNode_ = 0;
    uint32_t lastFixTimestamp_ = 0;
    Fix lastFix_{};
    float lastClearance_ = 0;
    uint32_t ownerGpsSecs_ = 0;
    uint32_t lastPacedSecs_ = UINT32_MAX; // nothing set yet: the first pass records the owner's value
    uint32_t lastStatusMs_ = 0;
    uint32_t lastHelloMs_ = 0;
    bool named_ = false;
    uint32_t lastCmdSeq_ = 0; // newest command accepted; older or repeated ones are replays
    uint16_t reportS_ = 0;    // report interval from the base, s; 0 = GEOCOLLAR_STATUS_INTERVAL_MS
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
