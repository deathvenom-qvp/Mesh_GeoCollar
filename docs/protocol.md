# GeoCollar wire protocol (v1)

GeoCollar messages travel as Meshtastic data packets on **portnum 300** (private apps use 256 and
up). All multi-byte fields are little-endian. Every message starts with:

| Byte | Field |
| --- | --- |
| 0 | protocol version, `1` |
| 1 | message type |

Messages between base and collar are **direct messages**, so Meshtastic encrypts them with PKI
(X25519 + AES-256-CCM). Relays can forward them but cannot read them. A collar ignores GeoCollar
messages that are not PKI-encrypted from a public key in its `security.admin_key` list.

The reference implementations are `firmware/GeoCollar/Protocol.cpp` and
`base/geocollar_base/protocol.py`; their tests share golden byte strings.

## 1. FenceChunk (base to collar)

| Offset | Size | Field |
| --- | --- | --- |
| 2 | 2 | fence version |
| 4 | 1 | chunk index (0-based) |
| 5 | 1 | chunk count (1..8) |
| 6 | ≤180 | chunk data |

Every chunk except the last carries exactly 180 bytes. Concatenated in index order, the chunks form
a **fence set**:

| Size | Field |
| --- | --- |
| 1 | zone count (0..4) |
| per zone: 1 | kind: 0 keep-in, 1 keep-out |
| per zone: 1 | vertex count (3..32) |
| per zone: 2 | warning margin, metres |
| per vertex: 4 + 4 | latitude, longitude as int32 degrees × 1e7 |

The collar saves a complete set to flash and reports its fence version in every Status, so the
base can resend until the versions match.

## 2. Status and 3. Breach (collar to base)

Same 20-byte layout; Breach is sent the moment the collar enters Lost, and every 30 s while lost.

| Offset | Size | Field |
| --- | --- | --- |
| 2 | 2 | fence version held |
| 4 | 1 | state: 0 no fence, 1 inside, 2 warning, 3 outside, 4 lost |
| 5 | 1 | battery % (255 unknown) |
| 6 | 4 | latitude, int32 × 1e7 |
| 10 | 4 | longitude, int32 × 1e7 |
| 14 | 2 | PDOP × 100 |
| 16 | 1 | satellites in view |
| 17 | 2 | clearance to nearest fence line, metres, signed (negative = on the wrong side) |
| 19 | 1 | flags: bit 0 corrections enabled, bit 1 shock enabled |

## 4. Command (base to collar)

| Offset | Size | Field |
| --- | --- | --- |
| 2 | 1 | action: 1 shock, 2 vibrate, 3 beep, 10 corrections off, 11 corrections on, 12 force lost, 13 clear lost, 14 request status |
| 3 | 1 | intensity 0..99 |
| 4 | 2 | duration, ms (the collar caps this at 3000) |

A manual shock is only sent when shock is enabled on the collar, and is capped at its maximum level.
