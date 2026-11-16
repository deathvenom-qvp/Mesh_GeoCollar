// SPDX-License-Identifier: GPL-3.0-or-later
//
// SHA-256 (FIPS 180-4) and HMAC-SHA256 (RFC 2104), for authenticating the companion app. Plain
// C++ with no hardware or library behind it, so the PC tests run the very code the collar runs.

#pragma once

#include <cstddef>
#include <cstdint>

namespace geocollar
{
namespace hash
{

constexpr size_t kSha256Len = 32;

class Sha256
{
  public:
    Sha256();
    void update(const uint8_t *data, size_t len);
    /// Writes the digest. The object must not be used again afterwards.
    void finish(uint8_t out[kSha256Len]);

  private:
    void compress(const uint8_t *block);
    uint32_t h_[8];
    uint64_t bytes_ = 0;
    uint8_t block_[64];
    size_t fill_ = 0;
};

void hmacSha256(const uint8_t *key, size_t keyLen, const uint8_t *msg, size_t msgLen, uint8_t out[kSha256Len]);

/// Compares two tags without stopping at the first difference, so the time taken says nothing
/// about how many leading bytes matched.
bool sameBytes(const uint8_t *a, const uint8_t *b, size_t len);

} // namespace hash
} // namespace geocollar
