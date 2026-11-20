# GeoCollar wire protocol (v2)

GeoCollar messages travel as Meshtastic data packets on **portnum 300** (private apps use 256 and
up). All multi-byte fields are little-endian. Every message starts with:

| Byte | Field |
| --- | --- |
| 0 | protocol version, `2` (v2 added per-zone correction bands; v1 messages are ignored) |
| 1 | message type |

Messages between base and collar are **direct messages**, so Meshtastic encrypts them with PKI
(X25519 + AES-256-CCM). Relays can forward them but cannot read them. A collar ignores GeoCollar
messages that are not PKI-encrypted from a public key in its `security.admin_key` list. The one
exception is the companion app, which reaches the collar over Bluetooth instead of the mesh and
proves itself with a key of its own (see [The companion app](#the-companion-app)).

The reference implementations are `firmware/GeoCollar/Protocol.cpp`, `firmware/GeoCollar/AppLink.cpp`,
`base/geocollar_base/protocol.py` and, for the app, `app/src/applink.js`; their tests share golden
byte strings.

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
base can resend until the versions match. It refuses a set whose version is older than the one it holds
(16-bit, wrapping), so an old set recorded off the air cannot replace newer fences; the base
numbers new sets after the version the collar last reported. A chunk of an older version is
dropped as it arrives, so it cannot throw away a newer set that is still coming in.

A set with the version and the content the collar already holds is not loaded again: the collar
answers with a Status and carries on. Loading it would restart the fence state, and with it the
shock limits and the lost beacon, so a recording of the current fences cannot be used to reset
them. The same version with different content is loaded (the base and the collar had lost step).

The collar checks its fences only against fixes from its own GPS. A position a phone app shares
with the collar, a fixed position, or one restored from flash after a restart is never used.

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
| 19 | 1 | flags: bit 0 corrections enabled, bit 1 shock enabled, bit 2 lost beacon on, bit 3 lost beacon silenced, bit 4 no current GPS fix, bit 5 the collar reads sequence numbers on Owner and Config, bit 6 the collar holds the key of at least one phone app (section 8), bit 7 home mode: near the home anchor with the GPS off (0.19.2 on) |

With bit 4 set the position is the last one the collar had (0, 0 if it has had none since it
started) and the clearance belongs to that position. A fix counts as current for the GPS interval
plus a minute, or for 45 s when the GPS runs all the time (an interval of 10 s or less). With
fences loaded the collar sends a Status when a fix goes missing and when it comes back, at most
one such report in 5 minutes, and keeps its GPS searching instead of letting Meshtastic rest it
for up to an hour after a failed search.

With bit 7 set the GPS is off because the collar hears its home anchor (see Config below): the
position and clearance are from its last fix, which showed the dog inside and clear of every
band. Bit 4 may be set too once that fix is old. The collar sends a Status when it enters and
leaves home mode.

A collar also sends a Status 15 s after it starts, so the base hears from it without waiting a
whole report interval.

## 4. Command (base to collar)

| Offset | Size | Field |
| --- | --- | --- |
| 2 | 1 | action: 1 shock, 2 vibrate, 3 beep, 10 corrections off, 11 corrections on, 12 force lost, 13 clear lost, 14 request status, 15 silence the lost beacon (until the dog is next back inside), 16 shock on at level `intensity` (1..99, capped by the collar's maximum, 30), 17 shock off |
| 3 | 1 | intensity 0..99 |
| 4 | 2 | duration, ms (the collar caps this at 3000) |
| 6 | 4 | sequence number: the base's clock in seconds, or one more than its last if the clock is behind |

The collar refuses a command whose sequence number is not higher than the last one it accepted
(saved across reboots), and one without a sequence number, so a command recorded off the air
cannot be played back. Golden bytes: vibrate 40 for 1500 ms, sequence 1763085600:
`02 04 02 28 dc05 208d1669`.

A manual shock is only sent when shock is enabled on the collar, and is capped at its maximum level.
With the collar's hardware switch off (GEOCOLLAR_DISABLE_PIN held low) manual beep, vibrate and
shock are refused as well as the fence's own corrections.

A correction lasts for its duration from the first frame the collar sends to the receiver. One
the fence started is cut short the moment a fix puts the dog past the line; one the owner sent
plays out.

## 5. Owner (base to collar)

The dog's name and the owner's contact, for the lost beacon. Three text fields, each a one-byte
length then that many bytes of UTF-8, with no control characters, then an optional 4-byte
sequence number (never 0). The message must be exactly this long, with or without the number.

| Field | Max bytes |
| --- | --- |
| dog's name (the collar's name in the base app; empty = "Dog") | 24 |
| owner's name | 24 |
| owner's phone | 20 |

Golden bytes for Rex / Ken / 555-123-4567: `02 05 03 526578 03 4b656e 0c 3535352d3132332d34353637`;
for Rex / Ken / 5551234567 with sequence number 1763085600:
`02 05 03 526578 03 4b656e 0a 35353531323334353637 208d1669`.

The sequence number comes from the same counter as a command's. The collar keeps the highest one
it has accepted for Owner messages (saved across reboots) and refuses a message whose number is
not higher, so a recording cannot put an old name or phone number back. Once it has accepted a
numbered message it refuses unnumbered ones too. Collar firmware before 0.16 refuses the longer
message, so the base adds the number only for a collar whose Status has flag bit 5, and sends
the message again, numbered, when such a collar last got it without one.

## 7. Config (base to collar)

| Offset | Size | Field |
| --- | --- | --- |
| 2 | 2 | report interval, seconds: 30..3600, or 0 for the firmware's default (10 minutes) |
| 4 | 4 | sequence number, optional (never 0) |
| 8 | 1 | home anchor kind, optional (only after a sequence number): 0 off, 1 Bluetooth beacon, 2 the base radio |
| 9 | 6 | the beacon's Bluetooth address, most significant byte first (zeros for the other kinds) |
| 15 | 1 | signal threshold, dBm, signed: -100..-30 |
| 16 | 1 | GPS interval inside the fences, optional (only after the anchor): bits 0-6 seconds, 0..30, 0 = always on; bit 7 that interval everywhere inside (0.19.6 on) |

How often the collar sends a Status. While lost it reports at least every 30 s whatever this
says. The sequence number works as for Owner, with its own counter on the collar. More bytes may
follow the number (room for later settings); 1 to 3 bytes after the interval are refused.
Firmware 0.15 reads the interval and ignores the rest, so the base always sends the number.
Golden bytes for 300 s: `02 07 2c01`, and with sequence number 1763085600: `02 07 2c01 208d1669`.

The home anchor (0.19.2 on) turns home mode on or off: while the collar hears the anchor at the
threshold or stronger, it turns its GPS off (see [hardware.md](hardware.md#home-mode)). A beacon
is matched by its address; the base radio by its Meshtastic Bluetooth name, which ends in `_`
and the last four hex digits of the node ID of the base that sent the collar its fences (so the
base radio needs no address). A kind above 2 or a threshold out of range makes the whole message
refused; fewer than 8 bytes after the number are ignored, as room for later settings. The collar
keeps the anchor in `/prefs/geocollar-home.bin`. Firmware before 0.19.2 ignores it. Golden bytes
for a beacon AA:BB:CC:01:02:03 at -72 dBm, interval 0, number 77:
`02 07 0000 4d000000 01 aabbcc010203 b8`.

The GPS interval inside the fences (0.19.6 on): the collar takes a fix this often while the dog is
inside, within 35 m of the outermost band, and not home (0 keeps the GPS on, a fix every second).
From 35 to 100 m the interval rises with the distance to 120 s; beyond 100 m it is 180 s. With
bit 7 set the interval holds wherever the dog is inside instead (the collar keeps that in its state
file, format 10); it never changes the pace past a line or while lost.
Just past a line it fixes at least every 5 s; lost mode keeps its own pace. A value above 30
makes the whole message refused. The collar keeps it with its other settings; one that has never
been sent it paces its GPS by the distance to the line, as before. The base always sends it, 3 s
unless the owner changed it, with an anchor of kind 0 in front when home mode was never set.
Firmware before 0.19.6 ignores it. Golden bytes for the base radio at -75 dBm, interval 0, number
77, every 3 s: `02 07 0000 4d000000 02 000000000000 b5 03`; every 3 s everywhere inside: the last
byte `83`.

## 6. Hello (collar, broadcast)

`02 06 <flags> <battery %>`: flags bit 0 the collar has an admin key, bit 1 it has fences;
battery 255 = unknown. Sent on the primary channel, unencrypted beyond the channel key, by a collar
that has not yet heard from a base it trusts: every minute for 15 minutes after boot, then every
15 minutes. The base lists the sender as a new collar. Golden bytes: `02 06 01 57`.

### The lost beacon

Once the dog has been past a line for 90 s (counted from the first good fix outside, and only
with fences loaded), the collar:

- keeps lost mode's GPS pace: a fix every 60 s while the base answers (directly or through the
  mesh) or the phone app is connected, the GPS off otherwise;
- advertises on Bluetooth as `<dog> is lost <phone>` for 90 s, then stops advertising for 90 s,
  and repeats. A collar that holds a phone app's key (section 8) does not go silent in the quiet
  90 s: it advertises its ordinary Meshtastic name every 2 s, so the owner's app can still find
  and connect to it. Spaces, dots and brackets are dropped from the phone number, and the name becomes
  `<dog> lost <phone>` and then a shortened dog's name if it would not fit Bluetooth's 29 bytes.
  It uses Meshtastic's own Bluetooth stack, advertising once a second, so Bluetooth must stay
  enabled in the collar's Meshtastic settings. While a phone is connected it leaves Bluetooth alone;
- broadcasts an ordinary Meshtastic text message straight away and then every 5 minutes:
  `Please help get <dog> home. Call <owner> <phone>. Last seen <lat>,<lon>`. It goes on the first
  channel with the well-known default key (or no key) so any Meshtastic user nearby can read it;
  without one, it goes on the primary channel.

It stops when a good fix is back inside every fence, when new fences arrive, or on command 15.
Silenced, it stays off until the dog has been back inside, so the next escape starts it again.

## 8. AppKeys (base to collar) and 9. AppKeysAck (collar to base)

The phones that may connect to this collar with the companion app (`app/`). Sent like every other
base message, as a PKI direct message from an admin key.

AppKeys:

| Offset | Size | Field |
| --- | --- | --- |
| 2 | 4 | sequence number, from the same counter as a command's (never 0) |
| 6 | 1 | key count, 0..4 |
| per key: 4 | key id (never 0, no two alike) |
| per key: 16 | the key |

It is always the whole list, so a phone left out is a phone removed, and an empty list removes
them all. The collar refuses a list whose sequence number is not higher than the last it accepted,
saves an accepted list to flash, ends a running session whose key is gone, and answers:

AppKeysAck: `02 09 <sequence number, 4 bytes> <key count>`.

The base sends the list again, at growing intervals up to an hour, each time the collar reports
until the acknowledgement for its newest list arrives, and also when a report's flag bit 6
disagrees with the list (a collar that was erased, or one that still holds keys this base never
gave it). Golden bytes for one key (id `0x1a2b3c4d`, key `00 01 .. 0f`), sequence 1763085600:
`02 08 208d1669 01 4d3c2b1a 000102030405060708090a0b0c0d0e0f`, and its acknowledgement
`02 09 208d1669 01`.

## 10. Anchor (collar to base, 0.19.16 on)

Whether the collar hears its home anchor (see Config), for the owner to see as "In Bluetooth
Range". It says so whatever the GPS, the fences or the dog's state, and does not wait the 20 s
home mode does.

| Offset | Size | Field |
| --- | --- | --- |
| 2 | 1 | flags: bit 0 in range |
| 3 | 1 | the latest reading of the anchor, dBm, signed; -128 if none in the last 15 s |
| 4 | 1 | the threshold the collar holds, dBm, signed |

In range from the first reading at the threshold or stronger, until no reading in 15 s is within
6 dB of it. The collar sends this when that changes (at most every 30 s; a change in between goes
out at the end of the 30 s), when it gets a new anchor, and with a status report when the last one
is more than 5 minutes old, while it has an anchor. A direct message with an acknowledgement,
like a status. The base checks the sender as for a status, and shows it while home mode is on and
the collar's reports keep coming with it. Bases before 0.19.16 ignore it. Golden bytes for in
range at -68 dBm, threshold -75: `02 0a 01 bc b5`; not heard: `02 0a 00 80 b5`.

## The companion app

A phone talks to the collar over the collar's own Meshtastic Bluetooth service, as any Meshtastic
phone app does: it writes `ToRadio` protobufs and reads `FromRadio` ones. GeoCollar messages go
in a `MeshPacket` addressed to the collar's own node number, on portnum 300. The collar's module
sees such a packet as coming from itself, answers with a packet from itself to itself, and hands
that to the phone only: nothing of this goes on the air.

Reaching the Bluetooth service at all takes the collar's Bluetooth PIN. That is not enough to
command the collar: every request is signed with a key that the base made for one phone, gave to
the phone in a QR code and to the collar in AppKeys. Messages are authenticated, not encrypted
(the Bluetooth link is encrypted by the pairing).

| Type | Direction | Layout after the 2-byte header |
| --- | --- | --- |
| 16 AppHello | app to collar | key id (4), app nonce (8) |
| 17 AppChallenge | collar to app | key id (4), collar nonce (8), tag (8) |
| 18 AppRequest | app to collar | key id (4), counter (4), action (1), intensity (1), duration ms (2), tag (8) |
| 19 AppStatus | collar to app | key id (4), counter (4), result (1), the 18 bytes of a Status after its header, age of the fix in seconds (2, `0xffff` = no fix yet), tag (8) |
| 20 AppError | collar to app | key id (4), code (1): 1 unknown key, 2 no session |

- Challenge tag: the first 8 bytes of HMAC-SHA256(key, `"GCA1"` + key id + app nonce + collar
  nonce). It proves to the app that the collar holds the key.
- Session key: HMAC-SHA256(key, `"GCS1"` + key id + app nonce + collar nonce), all 32 bytes.
- Request and status tags: the first 8 bytes of HMAC-SHA256(session key, the message up to the tag).

A hello starts a session and ends the one before it; the collar keeps one, in RAM. Both nonces
are fresh random bytes, so a recorded session is worthless later. Within a session the counter
starts at 1 and must grow with every request; the collar checks the tag first, so a forged
request cannot use a counter up. An AppStatus repeats its request's counter. Result is 0 done or
1 not allowed.

A phone may ask for these actions only: 14 request status, 15 silence the lost beacon, 3 beep,
2 vibrate, 10 corrections off, 11 corrections on. Anything else, shock included, gets result 1 and
is not done. Beep, vibrate, silence and the corrections switch run through the same code as a
command from the base, which also reports the change to the base; a request for status stays off
the mesh. An AppError is not signed: all it can do is make the app say hello again.

Golden bytes (key id `0x1a2b3c4d`, key `00 01 .. 0f`, app nonce `a0 .. a7`, collar nonce
`c0 .. c7`), shared by `firmware/test/test_main.cpp`, `base/tests/test_protocol.py` and
`app/test/protocol.test.mjs`:

| Message | Bytes |
| --- | --- |
| AppHello | `0210 4d3c2b1a a0a1a2a3a4a5a6a7` |
| AppChallenge | `0211 4d3c2b1a c0c1c2c3c4c5c6c7 c66cfaa9f4fe36cb` |
| session key | `4b4ad99bb6e9aa4a581e9cc9defdb082abb994605cae8f691fde30c639488fce` |
| AppRequest, counter 1, silence the lost beacon | `0212 4d3c2b1a 01000000 0f 00 0000 d2b8917361311478` |
| AppError, no session | `0214 4d3c2b1a 02` |

### The linking code (version 3, base 0.19.10 on)

The base's **Connect app** QR code is one opaque base64url string with no readable text in it:
version byte `3`, a 16-byte one-time secret, then AES-128-GCM (key and IV from
HKDF-SHA256(secret, info `"GeoCollar QR v3"`), associated data the version byte) of
`{"h": <base id>, "t": <8-byte token, base64url>, "u": [<addresses of the phone API>]}`. It holds
no lasting key: the phone uses it once, within 10 minutes, to fetch its keys from the base over
the network, encrypted, through the phone API. That API, its encryption and its schemas are in
[phone-api.openapi.json](phone-api.openapi.json) (OpenAPI 3.1; the reference implementations are
`base/geocollar_base/phoneapi.py` and `app/src/phoneapi.js`, with shared golden values). The
phone's key for the collars (below) comes in the enrolment answer instead of in the code.

### The pairing code (versions 1 and 2)

What the base's **Connect app** QR code said from base 0.19.7 to 0.19.9 (version 2). The app still
reads it, and the base still shows the collar list form of it for phones those bases linked. It
links a phone to every collar on the base:

`geocollar://pair?v=2&h=<base id, 8 hex digits>&i=<key id, 8 hex digits>&k=<phone key, base64url without padding>`

then, for each collar on the base, `&n=<node number, 8 hex digits>` followed by any of
`&p=<the collar's 6-digit Bluetooth PIN>`, `&b=<the collar's Meshtastic short name, which its
Bluetooth name starts with>`, `&d=<the dog's name>` and `&s=1` for one of the base's simulated
collars (the app then shows its demo collar, as there is nothing to connect to). A collar's fields
follow its `n=`, in order.

The phone key is the phone's for the whole base. Each collar's own key for that phone, the one in
its AppKeys, is the first 16 bytes of HMAC-SHA256(phone key, `"GCK1"` + node number, 4 bytes
little-endian); the key id is the same on every collar. The app works the same key out for each
collar. The phone key is in the code and nowhere else outside the base, so the base shows such a
code once. Golden bytes: phone key `00 01 .. 0f`, node `0xa1b2c3d4`: collar key
`faabcb3c8ddf0f9da74afdae47c96531`.

The same code without `i=` and `k=` is the base's list of collars, for a phone already linked to
that base (the app finds its phone key by the base id). It adds the collars added since, updates
the others' names and PINs, and drops the collars the base no longer lists. It holds no secret
beyond the Bluetooth PINs, so the base can show it again at any time.

Version 1, from bases before 0.19.7, links one collar, with that collar's own key, and the app
still reads it:

`geocollar://pair?v=1&n=<node number, 8 hex digits>&i=<key id, 8 hex digits>&k=<key, base64url without padding>`

followed by the same optional `p`, `b`, `d` and `s`.

