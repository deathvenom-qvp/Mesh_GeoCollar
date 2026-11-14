# GeoCollar wire protocol (v2)

GeoCollar messages travel as Meshtastic data packets on **portnum 300** (private apps use 256 and
up). All multi-byte fields are little-endian. Every message starts with:

| Byte | Field |
| --- | --- |
| 0 | protocol version, `2` (v2 added per-zone correction bands; v1 messages are ignored) |
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
| 1 | beep pulse: repeat beep every this many seconds while in the attention band (0 = once per GPS fix, else 2..10) |
| 1 | vibrate pulse: same for vibrate (0, or 2..10) |
| per zone: 1 | kind: 0 keep-in, 1 keep-out |
| per zone: 1 | vertex count (3..32) |
| per zone: 1 | danger band width, metres (0 = no danger band, never shock for this zone) |
| per zone: 1 | warning band width, metres |
| per zone: 1 | attention band width, metres |
| per zone: 1 | reserved, 0 |
| per vertex: 4 + 4 | latitude, longitude as int32 degrees × 1e7 |

The polygon is the outer edge of the zone's correction bands. Measured from the line into the
allowed side (inwards for keep-in, outwards for keep-out):

| Band | From the line | Correction |
| --- | --- | --- |
| danger | 0 to danger | shock, if shock is enabled on the collar and under its caps; otherwise vibrate |
| warning | danger to danger + warning | vibrate |
| attention | to danger + warning + attention | beep |
| clear | beyond that | nothing |

Past the line the collar stops correcting at once. After 3 good fixes outside it switches to lost
mode. Either way it stays silent, even while coming back in through the bands, until the dog is
inside and clear of every band (lost mode needs 3 such fixes). With several zones, the most severe
band of any zone wins.

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
| 19 | 1 | flags: bit 0 corrections enabled, bit 1 shock enabled, bit 2 lost beacon on, bit 3 lost beacon silenced |

## 4. Command (base to collar)

| Offset | Size | Field |
| --- | --- | --- |
| 2 | 1 | action: 1 shock, 2 vibrate, 3 beep, 10 corrections off, 11 corrections on, 12 force lost, 13 clear lost, 14 request status, 15 silence the lost beacon (until the dog is next back inside) |
| 3 | 1 | intensity 0..99 |
| 4 | 2 | duration, ms (the collar caps this at 3000) |

A manual shock is only sent when shock is enabled on the collar, and is capped at its maximum level.

## 5. Owner (base to collar)

The dog's name and the owner's contact, for the lost beacon. Three text fields, each a one-byte
length then that many bytes of UTF-8, with no control characters. The message must be exactly
this long.

| Field | Max bytes |
| --- | --- |
| dog's name (the collar's name in the base app; empty = "Dog") | 24 |
| owner's name | 24 |
| owner's phone | 20 |

Golden bytes for Rex / Ken / 555-123-4567: `02 05 03 526578 03 4b656e 0c 3535352d3132332d34353637`.

## 6. Hello (collar, broadcast)

`02 06 <flags> <battery %>`: flags bit 0 the collar has an admin key, bit 1 it has fences;
battery 255 = unknown. Sent on the primary channel, unencrypted beyond the channel key, by a collar
that has not yet heard from a base it trusts: every minute for 15 minutes after boot, then every
15 minutes. The base lists the sender as a new collar. Golden bytes: `02 06 01 57`.

### The lost beacon

Once the dog has been past a line for 90 s (counted from the first good fix outside, and only
with fences loaded), the collar:

- halves its GPS rate (twice the lost-mode interval, 30 s by default) to save battery;
- advertises on Bluetooth as `<dog> is lost <phone>` for 90 s, then stops advertising for 90 s,
  and repeats. Spaces, dots and brackets are dropped from the phone number, and the name becomes
  `<dog> lost <phone>` and then a shortened dog's name if it would not fit Bluetooth's 29 bytes.
  It uses Meshtastic's own Bluetooth stack, advertising once a second, so Bluetooth must stay
  enabled in the collar's Meshtastic settings. While a phone is connected it leaves Bluetooth alone;
- broadcasts an ordinary Meshtastic text message straight away and then every 5 minutes:
  `Please help get <dog> home. Call <owner> <phone>. Last seen <lat>,<lon>`. It goes on the first
  channel with the well-known default key (or no key) so any Meshtastic user nearby can read it;
  without one, it goes on the primary channel.

It stops when a good fix is back inside every fence, when new fences arrive, or on command 15.
Silenced, it stays off until the dog has been back inside, so the next escape starts it again.
