// SPDX-License-Identifier: GPL-3.0-or-later
//
// The collar's half of the companion app link as a command-line program, so the app's tests
// (app/test) can talk to the very code the collar runs instead of to a copy of it in JavaScript.
// Build with `make -C firmware/test applink_collar`.
//
// It reads one command per line and answers each with one line:
//   key <id, hex> <key, 32 hex digits>   authorize a phone             -> ok
//   revoke                               remove every phone            -> ok
//   nonce <16 hex digits>                the collar nonce for the next hello (else a counter) -> ok
//   msg <hex>                            a message from the app        -> reply <hex> | ignore
// A request that is allowed changes a small pretend collar: a lost dog whose beacon is on.

#include "../GeoCollar/AppLink.h"

#include <cstdio>
#include <cstring>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

using namespace geocollar;
using namespace geocollar::proto;

static bool unhex(const std::string &s, std::vector<uint8_t> &out)
{
    out.clear();
    if (s.size() % 2)
        return false;
    for (size_t i = 0; i < s.size(); i += 2) {
        unsigned v = 0;
        if (std::sscanf(s.substr(i, 2).c_str(), "%2x", &v) != 1)
            return false;
        out.push_back(static_cast<uint8_t>(v));
    }
    return true;
}

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

int main()
{
    AppKeySet keys;
    AppLink link;
    // Lost, 12 m past the line, beacon on, corrections enabled.
    StatusReport status{3, 4, 87, 400001234, -750004321, 180, 9, -12, kFlagCorrections | kFlagBeacon | kFlagNumbered};
    uint8_t nonce[kAppNonceLen] = {};
    uint64_t counter = 1;
    bool nonceSet = false;

    std::string line;
    while (std::getline(std::cin, line)) {
        std::istringstream in(line);
        std::string cmd, a, b;
        in >> cmd >> a >> b;
        std::vector<uint8_t> bytes;
        if (cmd == "key" && unhex(b, bytes) && bytes.size() == kAppKeyLen && keys.count < kMaxAppKeys) {
            AppKey &k = keys.keys[keys.count++];
            k.id = static_cast<uint32_t>(std::stoul(a, nullptr, 16));
            memcpy(k.key, bytes.data(), kAppKeyLen);
            status.flags |= kFlagApps;
            link.keysChanged(keys);
            std::cout << "ok" << std::endl;
        } else if (cmd == "revoke") {
            keys = AppKeySet();
            status.flags &= static_cast<uint8_t>(~kFlagApps);
            link.keysChanged(keys);
            std::cout << "ok" << std::endl;
        } else if (cmd == "nonce" && unhex(a, bytes) && bytes.size() == kAppNonceLen) {
            memcpy(nonce, bytes.data(), kAppNonceLen);
            nonceSet = true;
            std::cout << "ok" << std::endl;
        } else if (cmd == "msg" && unhex(a, bytes)) {
            if (!nonceSet) { // never the same twice
                for (size_t i = 0; i < kAppNonceLen; i++)
                    nonce[i] = static_cast<uint8_t>(counter >> (8 * i));
                counter += 0x0101010101010101ULL;
            }
            nonceSet = false;
            uint8_t reply[kAppStatusLength];
            size_t n = 0;
            AppRequest req;
            switch (link.handle(bytes.data(), bytes.size(), keys, nonce, reply, sizeof(reply), n, req)) {
            case AppLink::Outcome::Ignore:
                n = 0;
                break;
            case AppLink::Outcome::Reply:
                break;
            case AppLink::Outcome::Request: {
                AppResult result = AppResult::Done;
                if (!appActionAllowed(req.action)) {
                    result = AppResult::NotAllowed;
                } else if (req.action == CommandAction::SilenceLost) {
                    status.flags = static_cast<uint8_t>((status.flags & ~kFlagBeacon) | kFlagBeaconSilenced);
                } else if (req.action == CommandAction::CorrectionsOff) {
                    status.flags &= static_cast<uint8_t>(~kFlagCorrections);
                } else if (req.action == CommandAction::CorrectionsOn) {
                    status.flags |= kFlagCorrections;
                }
                n = link.status(reply, sizeof(reply), req, result, status, 7);
                break;
            }
            }
            if (n)
                std::cout << "reply " << hex(reply, n) << std::endl;
            else
                std::cout << "ignore" << std::endl;
        } else {
            std::cout << "error" << std::endl;
        }
    }
    return 0;
}
