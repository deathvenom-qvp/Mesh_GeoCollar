# SPDX-License-Identifier: GPL-3.0-or-later
"""Changing the base radio's Meshtastic settings from the page.

Name and hop limit only matter to the base. Region, preset and channels must match on the base
and every collar, or they stop hearing each other; and a collar locked to the base (managed mode)
can only be changed over the mesh, by the base. So a radio change goes to the collars first, over
the mesh while they can still hear the old settings, and to the base last.
"""

from __future__ import annotations

import secrets

DEFAULT_PSK = b"\x01"  # Meshtastic's well-known default key (LongFast)
PUBLIC_NAME = "LongFast"
MAX_CHANNELS = 8


class RadioError(ValueError):
    pass


def _enums():
    from meshtastic.protobuf import config_pb2

    L = config_pb2.Config.LoRaConfig
    return L.RegionCode, L.ModemPreset


def options() -> dict:
    regions, presets = _enums()
    # VERY_LONG_SLOW is deprecated in Meshtastic 2.x and recent firmware refuses it.
    return {"regions": [r for r in regions.keys() if r != "UNSET"],
            "presets": [p for p in presets.keys() if p != "VERY_LONG_SLOW"]}


def key_kind(psk: bytes) -> str:
    psk = bytes(psk)
    if psk == b"":
        return "none"
    if len(psk) == 1:
        return "default" if psk == DEFAULT_PSK else "simple"
    return "private"


def describe(node, user: dict) -> dict:
    """The editable settings of a Meshtastic node (the base's localNode)."""
    regions, presets = _enums()
    lora = node.localConfig.lora
    chans = list(node.channels or [])
    primary = chans[0].settings if chans else None
    public = any(c.role == 2 and bytes(c.settings.psk) == DEFAULT_PSK for c in chans[1:])  # 2 = SECONDARY
    return {
        "long_name": user.get("longName", ""),
        "short_name": user.get("shortName", ""),
        "region": regions.Name(lora.region),
        "preset": presets.Name(lora.modem_preset) if lora.use_preset else "",
        "hop_limit": lora.hop_limit or 3,
        "channel_name": primary.name if primary else "",
        "channel_key": key_kind(primary.psk) if primary else "default",
        "public_secondary": public,
        "custom_lora": not lora.use_preset,
    }


def validate(req: dict) -> dict:
    regions, presets = _enums()
    out = {}
    if "long_name" in req:
        name = " ".join(str(req["long_name"]).split())
        if not name or len(name.encode()) > 39:
            raise RadioError("the radio's name must be 1 to 39 bytes")
        out["long_name"] = name
    if "short_name" in req:
        short = str(req["short_name"]).strip()
        if not short or len(short.encode()) > 4:
            raise RadioError("the short name must be 1 to 4 characters")
        out["short_name"] = short
    if "region" in req:
        if req["region"] not in regions.keys() or req["region"] == "UNSET":
            raise RadioError("pick a region")
        out["region"] = req["region"]
    if "preset" in req:
        if req["preset"] not in presets.keys() or req["preset"] == "VERY_LONG_SLOW":
            raise RadioError("unknown preset")
        out["preset"] = req["preset"]
    if "hop_limit" in req:
        h = int(req["hop_limit"])
        if not 1 <= h <= 7:
            raise RadioError("hop limit must be 1 to 7")
        out["hop_limit"] = h
    if "channel_key" in req:
        if req["channel_key"] not in ("keep", "default", "new"):
            raise RadioError("channel key must be keep, default or new")
        out["channel_key"] = req["channel_key"]
    if "channel_name" in req:
        cname = str(req["channel_name"]).strip()
        if len(cname.encode()) > 11 or not all(ch.isalnum() or ch in "-_" for ch in cname):
            raise RadioError("the channel name is at most 11 letters, digits, - or _")
        out["channel_name"] = cname
    if "public_secondary" in req:
        out["public_secondary"] = bool(req["public_secondary"])
    return out


def primary_moves(node, lora, chans) -> bool:
    """Whether the primary channel's identity (its key, or the name it is known by, which is the
    preset's when blank) changes: radios that have switched then acknowledge with a key the others
    can't read yet."""
    old = node.channels[0].settings if node.channels else None
    new = chans[0].settings
    if old is None or bytes(old.psk) != bytes(new.psk) or old.name != new.name:
        return True
    return not new.name and node.localConfig.lora.modem_preset != lora.modem_preset


def plan(node, req: dict):
    """New (lora, channels) for the request, starting from the node's current ones. Returns
    (lora, channels, mesh_changed): mesh_changed is True when collars must follow."""
    from meshtastic.protobuf import channel_pb2, config_pb2

    regions, presets = _enums()
    lora = config_pb2.Config.LoRaConfig()
    lora.CopyFrom(node.localConfig.lora)
    chans = []
    for i in range(MAX_CHANNELS):
        c = channel_pb2.Channel(index=i)
        if i < len(node.channels or []):
            c.CopyFrom(node.channels[i])
            c.index = i
        chans.append(c)
    before = (lora.SerializeToString(), [c.SerializeToString() for c in chans])

    if "region" in req:
        lora.region = regions.Value(req["region"])
    if "preset" in req:
        lora.use_preset = True
        lora.modem_preset = presets.Value(req["preset"])
    if "hop_limit" in req:
        lora.hop_limit = req["hop_limit"]
    lora.tx_enabled = True

    p = chans[0]
    p.role = channel_pb2.Channel.Role.PRIMARY
    key = req.get("channel_key", "keep")
    if key == "default":
        p.settings.psk = DEFAULT_PSK
        p.settings.name = ""  # the default channel is unnamed (shown as the preset's name)
    elif key == "new":
        # A blank name keeps the default frequency slot (radios hash the primary channel's name, shown
        # as the preset's when blank), so a public secondary channel still reaches strangers.
        p.settings.psk = secrets.token_bytes(32)
        p.settings.name = req.get("channel_name", "")
    elif "channel_name" in req and key_kind(p.settings.psk) != "default":
        p.settings.name = req["channel_name"]

    if "public_secondary" in req:
        idx = [c.index for c in chans[1:] if c.role == 2 and bytes(c.settings.psk) == DEFAULT_PSK]
        primary_public = bytes(p.settings.psk) == DEFAULT_PSK
        if req["public_secondary"] and not idx and not primary_public:
            free = next((c for c in chans[1:] if c.role == 0), None)
            if free is None:
                raise RadioError("all 8 channels are in use; remove one in the Meshtastic app first")
            free.role = channel_pb2.Channel.Role.SECONDARY
            free.settings.psk = DEFAULT_PSK
            free.settings.name = PUBLIC_NAME
        elif not req["public_secondary"]:
            for c in chans[1:]:
                if c.index in idx:
                    c.role = channel_pb2.Channel.Role.DISABLED
                    c.settings.Clear()

    # hop_limit (and switching transmit back on) is the base's own business; collars only need
    # region, preset and channels to match.
    a, b = config_pb2.Config.LoRaConfig(), config_pb2.Config.LoRaConfig()
    a.ParseFromString(before[0])
    b.CopyFrom(lora)
    a.hop_limit = b.hop_limit = 0
    a.tx_enabled = b.tx_enabled = True
    mesh_changed = a.SerializeToString() != b.SerializeToString() or before[1] != [c.SerializeToString() for c in chans]
    return lora, chans, mesh_changed
