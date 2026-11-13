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

using namespace geocollar;

namespace
{

constexpr const char *kStateFile = "/prefs/geocollar.bin";
constexpr uint32_t kStateMagic = 0x47434F4C; // "GCOL"
constexpr uint16_t kStateFormat = 2; // 2: Settings gained the GPS pacing fields

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
                if (proto::decodeFenceSet(assembler_.data(), assembler_.length(), assembler_.version(), fences)) {
                    engine_.setFences(fences);
                    saveState();
                    LOG_INFO("GeoCollar: fence v%u loaded, %u zones", fences.version, fences.zoneCount);
                    sendStatus(proto::MsgType::Status);
                }
            }
        } else if (type == proto::MsgType::Command) {
            proto::Command cmd;
            if (proto::decodeCommand(buf, len, cmd))
                handleCommand(cmd);
        }
        return ProcessMessage::STOP;
    }

    int32_t runOnce() override
    {
        const int32_t txDelay = tx_.poll();
        if (tx_.busy())
            return txDelay; // keep frames flowing; position checks resume once the collar is quiet

        checkPosition();
        applyGpsPacing();

        const uint32_t now = millis();
        const uint32_t interval =
            engine_.state() == CollarState::Lost ? GEOCOLLAR_LOST_STATUS_INTERVAL_MS : GEOCOLLAR_STATUS_INTERVAL_MS;
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
        const uint32_t want =
            gpsIntervalSecs(engine_.settings(), engine_.state(), lastClearance_, warnMarginM(engine_.fences()));
        const uint32_t target = want ? want : ownerGpsSecs_;
        if (current != target) {
            LOG_DEBUG("GeoCollar: GPS interval %us -> %us", (unsigned)current, (unsigned)target);
            current = target;
        }
        lastPacedSecs_ = current;
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
            saveState();
            break;
        case proto::CommandAction::CorrectionsOn:
            s.correctionsEnabled = true;
            saveState();
            break;
        case proto::CommandAction::ForceLost:
            engine_.forceLost();
            tx_.stop();
            break;
        case proto::CommandAction::ClearLost:
            engine_.clearLost();
            break;
        case proto::CommandAction::RequestStatus:
            break;
        }
        sendStatus(proto::MsgType::Status);
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
        s.flags = (engine_.settings().correctionsEnabled ? 1 : 0) | (engine_.settings().shockEnabled ? 2 : 0);

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
        bool ok = f.read((uint8_t *)&magic, sizeof(magic)) == sizeof(magic) && magic == kStateMagic &&
                  f.read((uint8_t *)&format, sizeof(format)) == sizeof(format) && format == kStateFormat &&
                  f.read((uint8_t *)&base, sizeof(base)) == sizeof(base) &&
                  f.read((uint8_t *)&version, sizeof(version)) == sizeof(version) &&
                  f.read((uint8_t *)&len16, sizeof(len16)) == sizeof(len16) && len16 <= sizeof(blob) &&
                  f.read(blob, len16) == len16 && f.read((uint8_t *)&settings, sizeof(settings)) == sizeof(settings);
        f.close();
        FenceSet fences;
        if (ok && proto::decodeFenceSet(blob, len16, version, fences)) {
            engine_.setFences(fences);
            engine_.settings() = settings;
            baseNode_ = base;
        } else {
            LOG_WARN("GeoCollar: saved state unreadable, starting without fences");
        }
    }

    FenceEngine engine_;
    CollarTransmitter tx_;
    proto::FenceAssembler assembler_;
    NodeNum baseNode_ = 0;
    uint32_t lastFixTimestamp_ = 0;
    Fix lastFix_{};
    float lastClearance_ = 0;
    uint32_t ownerGpsSecs_ = 0;
    uint32_t lastPacedSecs_ = UINT32_MAX; // nothing set yet: the first pass records the owner's value
    uint32_t lastStatusMs_ = 0;
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
