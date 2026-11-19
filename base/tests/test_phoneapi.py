# SPDX-License-Identifier: GPL-3.0-or-later
"""The phone API: linking a phone with the one-time QR code, the encrypted requests, collar
reports passed on by a phone, the Base URL setting and the OpenAPI document."""

import json
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from geocollar_base import phoneapi as papi
from geocollar_base.app import create_app

from test_app import FakeLink, GOOD

SPEC = Path(__file__).resolve().parents[2] / "docs" / "phone-api.openapi.json"


def make(tmp_path, **settings):
    link = FakeLink()
    client = TestClient(create_app({"data_dir": str(tmp_path), "phone_port": "8100", **settings}, link=link))
    client.post("/api/collars", json={"node_id": "!a1b2c3d4", "name": "Rex"})
    return client, link


def new_phone(client, name="Ken"):
    body = client.post("/api/phones", json={"name": name}).json()
    phone = papi.TestPhone(client, body["code"])
    return body, phone


def test_a_code_links_one_phone_once(tmp_path):
    client, _ = make(tmp_path)
    body, phone = new_phone(client)
    # The QR code is one opaque blob: nothing in it reads as an address, a name or a key.
    assert set(body["code"]) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert phone.code["urls"] == body["urls"] and len(phone.code["home"]) == 8
    # Someone without the code's secret can't use its token.
    other = papi.TestPhone(client, body["code"])
    other.code = {**other.code, "secret": bytes(16)}
    assert other.enroll().json()["code"] == "bad_request"
    # The phone that scanned it is linked.
    assert phone.enroll().status_code == 200
    assert phone.welcome["phone_id"] == body["phone"]["id"] and phone.welcome["collars"][0]["dog"] == "Rex"
    row = client.get("/api/phones").json()["phones"][0]
    assert row["api"] is True and row["enrolled"] and row["code_expires"] is None
    # Nobody else can use the code afterwards, even with the secret.
    again = papi.TestPhone(client, body["code"])
    r = again.enroll()
    assert r.status_code == 410 and r.json()["code"] == "used"
    # The keys never leave the base by any other way.
    secrets_ = [phone.key.hex(), papi.b64e(phone.key), phone.welcome["ble_key"]]
    for path in ("/api/phones", "/api/collars", "/api/phone-access"):
        assert not any(x in client.get(path).text for x in secrets_)


def test_a_code_runs_out(tmp_path, monkeypatch):
    client, _ = make(tmp_path)
    body, phone = new_phone(client)
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + papi.ENROLL_TTL_S + 1)
    r = phone.enroll()
    assert r.status_code == 410 and r.json()["code"] == "expired"
    assert client.get("/api/phones").json()["phones"][0]["code_expires"] is None
    # A new code for the same phone: it keeps its key for the collars, so they need nothing new.
    r = client.post(f"/api/phones/{body['phone']['id']}/code").json()
    phone = papi.TestPhone(client, r["code"])
    assert phone.enroll().status_code == 200 and phone.welcome["ble_key_id"] == int(body["phone"]["id"], 16)
    assert client.post("/api/phones/12345678/code").status_code == 404


def test_a_new_code_replaces_the_phones_api_key(tmp_path):
    client, _ = make(tmp_path)
    body, old = new_phone(client)
    old.enroll()
    assert old.call("collars")[0] == 200
    new = papi.TestPhone(client, client.post(f"/api/phones/{body['phone']['id']}/code").json()["code"])
    # The old one still works until the new code is used.
    assert old.call("collars")[0] == 200
    new.enroll()
    assert new.welcome["ble_key"] == old.welcome["ble_key"]
    assert old.call("collars") == (401, {"detail": old.call("collars")[1]["detail"], "code": "unknown_phone"})
    assert new.call("collars")[0] == 200


def test_requests_are_sealed_and_checked(tmp_path, monkeypatch):
    client, _ = make(tmp_path)
    _, phone = new_phone(client)
    phone.enroll()
    client.app.state.on_status("!a1b2c3d4", {**GOOD, "state": "LOST", "battery_pct": 81, "sats": 13})
    code, reply = phone.call("collars")
    assert code == 200 and reply["home"] and reply["urls"]
    rex = reply["collars"][0]
    assert rex["node_id"] == "!a1b2c3d4" and rex["dog"] == "Rex"
    assert rex["status"]["state"] == "LOST" and rex["status"]["lat"] == 40.0 and rex["status"]["via"] == "mesh"
    assert rex["status"]["battery_pct"] == 81 and abs(rex["status"]["received"] - time.time()) < 5
    assert "pki" not in rex["status"] and "sender_key" not in rex["status"]
    papi.CollarsReply.model_validate(reply)

    # What goes over the wire can't be read.
    iv = bytes(range(12))
    env = papi.seal(phone.key, papi.request_aad("collars", phone.phone), {"ts": int(time.time() * 1000), "body": {}}, iv)
    raw = client.post(papi.PREFIX + "/collars", json={"phone": phone.phone, **env})
    assert raw.status_code == 200 and set(raw.json()) == {"iv", "ct"} and "Rex" not in raw.text and "40.0" not in raw.text
    # The same request again is refused.
    assert client.post(papi.PREFIX + "/collars", json={"phone": phone.phone, **env}).json()["code"] == "replay"
    # A request sealed for another operation can't be played back as this one.
    env = papi.seal(phone.key, papi.request_aad("relay", phone.phone), {"ts": int(time.time() * 1000), "body": {}})
    assert client.post(papi.PREFIX + "/collars", json={"phone": phone.phone, **env}).json()["code"] == "unknown_phone"
    # Nor one from a phone that is not linked, or a changed one.
    assert client.post(papi.PREFIX + "/collars", json={"phone": "00000001", **env}).status_code == 401
    env = papi.seal(phone.key, papi.request_aad("collars", phone.phone), {"ts": int(time.time() * 1000), "body": {}})
    env["ct"] = env["ct"][:-2] + ("A" if env["ct"][-2] != "A" else "B") + env["ct"][-1]
    assert client.post(papi.PREFIX + "/collars", json={"phone": phone.phone, **env}).status_code == 401
    assert client.post(papi.PREFIX + "/collars", json={"phone": "nothex!!", "iv": "", "ct": ""}).status_code == 422
    # A phone whose clock is off is told the base's time, and can try again with it.
    code, err = phone.call("collars", ts_ms=int((time.time() - 3600) * 1000))
    assert code == 401 and err["code"] == "clock" and abs(err["server_time"] / 1000 - time.time()) < 5
    # Too big to be anything the app sends.
    r = client.post(papi.PREFIX + "/collars", content=b"x" * (papi.MAX_BODY + 1), headers={"content-type": "application/json"})
    assert r.status_code == 413
    # The phone API is open to the app from any origin; the page's own API is not.
    assert client.post(papi.PREFIX + "/collars", json={"phone": phone.phone, **papi.seal(phone.key, papi.request_aad(
        "collars", phone.phone), {"ts": int(time.time() * 1000), "body": {}})}, headers={"origin": "https://localhost"}).status_code == 200
    assert client.post("/api/phones", json={}, headers={"origin": "https://evil.example"}).status_code == 403
    pre = client.options(papi.PREFIX + "/collars", headers={"origin": "https://localhost", "access-control-request-method": "POST"})
    assert pre.status_code == 204 and pre.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-origin" not in client.get("/api/collars", headers={"origin": "https://evil.example"}).headers


def test_removing_a_phone_locks_it_out_at_once(tmp_path):
    client, _ = make(tmp_path)
    body, phone = new_phone(client)
    phone.enroll()
    assert client.get("/api/phones").json()["phones"][0]["last_seen"] is None
    assert phone.call("collars")[0] == 200
    assert client.get("/api/phones").json()["phones"][0]["last_seen"]
    client.delete(f"/api/phones/{body['phone']['id']}")
    code, err = phone.call("collars")
    assert code == 401 and err["code"] == "unknown_phone"
    # And its code, had it not been used yet, is gone too.
    body, phone = new_phone(client)
    client.delete(f"/api/phones/{body['phone']['id']}")
    assert phone.enroll().json()["code"] == "expired"


def test_a_phone_gets_collars_added_later_by_itself(tmp_path):
    client, _ = make(tmp_path)
    _, phone = new_phone(client)
    phone.enroll()
    client.post("/api/collars", json={"node_id": "!0badf00d", "name": "Bella"})
    assert client.get("/api/phones").json()["phones"][0]["missing"] == ["!0badf00d"]
    _, reply = phone.call("collars")
    assert [c["dog"] for c in reply["collars"]] == ["Bella", "Rex"]
    assert client.get("/api/phones").json()["phones"][0]["missing"] == []


def relay_body(**status):
    return {"node_id": "!a1b2c3d4", "fix_age_s": 4, "age_ms": 500,
            "status": {"state": "LOST", "lat": 40.01, "lon": -75.02, "battery_pct": 64, "sats": 14, "pdop": 1.5,
                       "clearance_m": -120, "beacon_active": True, **status},
            "phone_position": {"lat": 40.0101, "lon": -75.0201, "accuracy_m": 6}}


def test_a_phone_passes_on_a_collar_that_cannot_reach_the_base(tmp_path, monkeypatch):
    client, link = make(tmp_path)
    _, phone = new_phone(client, "Ken's phone")
    phone.enroll()
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    phone.clock_ms = None

    # The base hears the collar itself: the phone's copy is not needed.
    client.app.state.on_status("!a1b2c3d4", {**GOOD, "state": "LOST"})
    code, r = phone.call("relay", relay_body(), ts_ms=int(now[0] * 1000))
    assert code == 200 and r == {"recorded": False, "reason": "heard_over_mesh", "next_s": 120}

    # A lost collar reports every 30 s; after two missed reports and a minute, the phone's copy counts.
    client.app.state.store.set_collar_info("!a1b2c3d4", "send_error", {"reason": "MAX_RETRANSMIT", "at": now[0]})
    now[0] += 125
    code, r = phone.call("relay", relay_body(), ts_ms=int(now[0] * 1000))
    assert r == {"recorded": True, "reason": "recorded", "next_s": 30}
    c = client.get("/api/collars").json()["!a1b2c3d4"]
    s = c["status"]
    assert s["via"] == "phone" and s["via_phone"] == "Ken's phone" and s["lat"] == 40.01 and s["fix_age_s"] == 4
    assert s["state"] == "LOST" and s["beacon_active"] is True and s["no_fix"] is False
    assert s["phone_pos"] == {"lat": 40.0101, "lon": -75.0201, "accuracy_m": 6.0}
    assert c["track"][-1][1:] == [40.01, -75.02]
    assert c["send_error"]["reason"] == "MAX_RETRANSMIT"  # it says nothing about the mesh
    entry = client.get("/api/collars/!a1b2c3d4/log").json()["entries"][-1]
    assert entry["via"] == "phone" and entry["lat"] == 40.01
    # Other phones see where it was passed on from.
    _, other = new_phone(client, "Sam")
    other.enroll()
    rex = other.call("collars", ts_ms=int(now[0] * 1000))[1]["collars"][0]
    assert rex["status"]["via"] == "phone" and rex["status"]["via_phone"] == "Ken's phone"

    # Not more than every 30 s, and still while the mesh stays quiet.
    now[0] += 10
    assert phone.call("relay", relay_body(), ts_ms=int(now[0] * 1000))[1]["reason"] == "too_soon"
    now[0] += 25
    assert phone.call("relay", relay_body(), ts_ms=int(now[0] * 1000))[1]["recorded"] is True
    # A stale GPS fix shows as the last known position, not where the dog is now.
    now[0] += 30
    phone.call("relay", {**relay_body(), "fix_age_s": 600}, ts_ms=int(now[0] * 1000))
    assert client.get("/api/collars").json()["!a1b2c3d4"]["status"]["no_fix"] is True
    # Something the phone heard long ago is not passed on.
    now[0] += 30
    assert phone.call("relay", {**relay_body(), "age_ms": 300_000}, ts_ms=int(now[0] * 1000))[1]["reason"] == "too_old"

    # The collar gets through again: the phone's copies stop counting.
    client.app.state.on_status("!a1b2c3d4", {**GOOD, "state": "LOST"})
    now[0] += 30
    assert phone.call("relay", relay_body(), ts_ms=int(now[0] * 1000))[1]["reason"] == "heard_over_mesh"
    assert client.get("/api/collars").json()["!a1b2c3d4"]["status"].get("via") is None

    # Damaged or unknown.
    assert phone.call("relay", {"node_id": "!a1b2c3d4", "status": {"state": "ON_MARS"}}, ts_ms=int(now[0] * 1000))[0] == 400
    assert phone.call("relay", {**relay_body(), "node_id": "!00000001"}, ts_ms=int(now[0] * 1000))[1]["reason"] == "unknown_collar"


def test_a_collar_inside_the_fence_is_quiet_for_longer_first(tmp_path, monkeypatch):
    client, _ = make(tmp_path)
    _, phone = new_phone(client)
    phone.enroll()
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    client.app.state.on_status("!a1b2c3d4", GOOD)  # inside: a report every 10 minutes
    now[0] += 600
    assert phone.call("relay", relay_body(state="INSIDE"), ts_ms=int(now[0] * 1000))[1]["reason"] == "heard_over_mesh"
    now[0] += 700
    assert phone.call("relay", relay_body(state="INSIDE"), ts_ms=int(now[0] * 1000))[1]["recorded"] is True


def test_base_url_setting(tmp_path):
    client, _ = make(tmp_path)
    r = client.get("/api/phone-access", headers={"host": "192.168.1.20:8099"})
    a = r.json()
    assert a["url"] == "" and a["port"] == 8100 and a["urls"][0] == "http://192.168.1.20:8100"
    assert any("home network" in w for w in a["warnings"])  # nothing that works from outside
    for bad in ("ftp://x", "http://", "https://user:pw@host", "http://host:99999", "http://h/?q=1"):
        assert client.put("/api/phone-access", json={"url": bad}).status_code == 400, bad
    a = client.put("/api/phone-access", json={"url": " geocollar.example.com:8443/ "}, headers={"host": "192.168.1.20:8099"}).json()
    assert a["url"] == "http://geocollar.example.com:8443"
    assert a["urls"][:2] == ["http://geocollar.example.com:8443", "http://192.168.1.20:8100"]
    assert not a["warnings"] and a["items"][0]["source"] == "set"
    # The code a phone scans carries the addresses, and so does every answer.
    body, phone = new_phone(client)
    assert phone.code["urls"][0] == "http://geocollar.example.com:8443"
    phone.enroll()
    assert phone.welcome["urls"][0] == "http://geocollar.example.com:8443"
    client.put("/api/phone-access", json={"url": "https://dogs.example.net"})
    assert phone.call("collars")[1]["urls"][0] == "https://dogs.example.net"
    assert client.put("/api/phone-access", json={"url": ""}).json()["url"] == ""


def test_behind_a_reverse_proxy(tmp_path):
    """Nginx Proxy Manager and the like: https on 443, a host name or a path of its own in front."""
    client, _ = make(tmp_path)
    a = client.put("/api/phone-access", json={"url": "https://ha.example.com/geocollar/"}).json()
    assert a["urls"][0] == "https://ha.example.com/geocollar" and not a["warnings"]
    body, phone = new_phone(client)
    assert phone.code["urls"][0] == "https://ha.example.com/geocollar"
    # The proxy forwards /geocollar/api/phone/v1/... as it is: the base finds its API behind the prefix.
    assert phone.enroll(path="/geocollar" + papi.PREFIX + "/enroll").status_code == 200
    env = papi.seal(phone.key, papi.request_aad("collars", phone.phone), {"ts": int(time.time() * 1000), "body": {}})
    r = TestClient(client.app.state.phone_app).post("/geocollar" + papi.PREFIX + "/collars", json={"phone": phone.phone, **env})
    assert r.status_code == 200
    # The prefix trick only reaches the phone API, never the page's own.
    assert client.post("/geocollar/api/phones", json={}).status_code == 404


def supervisor(config, addon_port=8100, ip="192.168.1.5", posted=None):
    def fetch(url, body=None):
        if body is not None:
            posted.append((url, body))
            return {"result": "ok", "data": {"uuid": "x"}}
        if url.endswith("/core/api/config"):
            return config
        if url.endswith("/addons/self/info"):
            return {"data": {"hostname": "a0d7b954-geocollar-base", "network": {"8099/tcp": None, "8100/tcp": addon_port}}}
        if url.endswith("/network/info"):
            return {"data": {"interfaces": [{"primary": True, "ipv4": {"address": [f"{ip}/24"]}}]}}
        raise AssertionError(url)
    return fetch


def test_base_url_defaults_to_home_assistants_url_then_the_ip(tmp_path):
    fetch = supervisor({"external_url": "https://ha.example.com", "internal_url": "http://homeassistant.local:8123"})
    client, _ = make(tmp_path, supervisor_fetch=fetch)
    a = client.get("/api/phone-access").json()
    assert a["urls"] == ["http://ha.example.com:8100", "http://homeassistant.local:8100", "http://192.168.1.5:8100"]
    assert [i["source"] for i in a["items"]] == ["external_ha", "internal_ha", "ip"]
    assert len(a["warnings"]) == 1 and "integration is not set up" in a["warnings"][0]
    # The owner's own address goes first.
    a = client.put("/api/phone-access", json={"url": "https://dogs.example.net"}).json()
    assert a["urls"] == ["https://dogs.example.net", "http://ha.example.com:8100", "http://homeassistant.local:8100"]


def test_base_url_falls_back_to_the_ip_and_says_why(tmp_path):
    fetch = supervisor({"external_url": "https://abcdef.ui.nabu.casa", "internal_url": ""}, addon_port=None)
    client, _ = make(tmp_path, supervisor_fetch=fetch)
    a = client.get("/api/phone-access").json()
    assert a["urls"] == ["http://192.168.1.5:8100"]
    text = " ".join(a["warnings"])
    assert "integration is not set up" in text and "turned off" in text and "home network" in text
    # The host port Home Assistant publishes the phone API on is the one phones are given.
    client, _ = make(tmp_path / "2", supervisor_fetch=supervisor({"external_url": "", "internal_url": ""}, addon_port=18100))
    assert client.get("/api/phone-access").json()["urls"] == ["http://192.168.1.5:18100"]


def test_through_home_assistants_own_address(tmp_path):
    """The GeoCollar integration passes the phone API on at Home Assistant's own address (a reverse
    proxy's or Home Assistant Cloud's), and tells the base so: no port or proxy host of its own."""
    fetch = supervisor({"external_url": "", "internal_url": "http://homeassistant.local:8123"}, addon_port=None)
    client, _ = make(tmp_path, supervisor_fetch=fetch)
    r = client.put("/api/ha-link", json={"external": "https://abcdef.ui.nabu.casa/api/geocollar/phone",
                                         "internal": "http://homeassistant.local:8123/api/geocollar/phone"})
    a = r.json()
    assert r.status_code == 200 and a["through_ha"] is True
    assert a["urls"] == ["https://abcdef.ui.nabu.casa/api/geocollar/phone", "http://homeassistant.local:8123/api/geocollar/phone"]
    assert not a["warnings"]  # the port being off no longer matters
    # Phones are given those, and the base answers behind the path.
    body, phone = new_phone(client)
    assert phone.code["urls"][0] == "https://abcdef.ui.nabu.casa/api/geocollar/phone"
    assert phone.enroll(path="/api/geocollar/phone" + papi.PREFIX + "/enroll").status_code == 200
    # A Base URL set by hand still comes first.
    assert client.put("/api/phone-access", json={"url": "https://dogs.example.net"}).json()["urls"][0] == "https://dogs.example.net"
    # Only addresses with the integration's path are taken.
    assert client.put("/api/ha-link", json={"external": "https://evil.example/"}).status_code == 400
    # An integration that stopped saying so for hours no longer counts.
    st = client.app.state.store
    st.set_setting("ha_link", {**st.setting("ha_link"), "at": time.time() - 4 * 3600})
    client.put("/api/phone-access", json={"url": ""})
    assert client.get("/api/phone-access").json()["through_ha"] is False


def test_the_integration_is_put_in_place_and_announced(tmp_path):
    posted = []
    fetch = supervisor({"external_url": "", "internal_url": ""}, posted=posted)
    client, _ = make(tmp_path, supervisor_fetch=fetch, ha_config_dir=str(tmp_path / "ha"))
    # No access to Home Assistant's config folder: nothing is written, the hint says how to do it by hand.
    client.app.state.ha_setup()
    assert client.get("/api/phone-access").json()["integration"] == "no_access"
    assert posted == [("http://supervisor/discovery", {"service": "geocollar", "config": {
        "host": "a0d7b954-geocollar-base", "phone_port": 8100, "page_port": 8099}})]
    (tmp_path / "ha").mkdir()
    client.app.state.ha_setup()
    dest = tmp_path / "ha" / "custom_components" / "geocollar"
    assert (dest / "__init__.py").exists() and (dest / "config_flow.py").exists()
    a = client.get("/api/phone-access").json()
    assert a["integration"] == "installed" and "restart Home Assistant" in " ".join(a["warnings"])
    client.app.state.ha_setup()
    assert client.get("/api/phone-access").json()["integration"] == "current"
    # An older one is replaced; a folder of that name that is not ours is left alone.
    m = json.loads((dest / "manifest.json").read_text())
    (dest / "manifest.json").write_text(json.dumps({**m, "version": "0.0.1"}))
    assert papi.install_integration(tmp_path / "ha") == "updated" and papi.integration_version(dest) == m["version"]
    (dest / "manifest.json").write_text("{}")
    assert papi.install_integration(tmp_path / "ha") == "not_ours"


def test_the_phone_port_serves_the_phone_api_alone(tmp_path):
    client, _ = make(tmp_path)
    phone_only = TestClient(client.app.state.phone_app)
    assert phone_only.get("/").status_code == 404 and phone_only.get("/api/collars").status_code == 404
    assert phone_only.post("/api/phones", json={}).status_code == 404
    body = client.post("/api/phones", json={"name": "Ken"}).json()
    phone = papi.TestPhone(phone_only, body["code"])
    assert phone.enroll().status_code == 200 and phone.call("collars")[0] == 200
    assert phone_only.get(papi.PREFIX + "/openapi.json").status_code == 200


def test_openapi_document_is_valid_and_up_to_date(tmp_path):
    from openapi_spec_validator import validate

    client, _ = make(tmp_path)
    doc = TestClient(client.app.state.phone_app).get(papi.PREFIX + "/openapi.json").json()
    validate(doc)
    schemas = doc["components"]["schemas"]
    # Every operation names the schemas of what travels inside its envelopes, and they are there.
    for path, item in doc["paths"].items():
        for ref in item["post"]["x-plaintext"].values():
            assert ref["$ref"].split("/")[-1] in schemas, path
    text = json.dumps(doc)
    for ref in {r.split('"')[0] for r in text.split('"$ref": "')[1:]}:
        assert ref.split("/")[-1] in schemas, ref
    # docs/phone-api.openapi.json is this document (regenerate it with the line in the file's test).
    if not SPEC.exists() or json.loads(SPEC.read_text()) != doc:
        SPEC.write_text(json.dumps(doc, indent=2) + "\n")
        pytest.fail("docs/phone-api.openapi.json was out of date and has been rewritten: commit it")
    # The page's own API document includes the phone API, with the same schemas.
    full = client.get("/openapi.json").json()
    validate(full)
    assert papi.PREFIX + "/relay" in full["paths"] and "RelayRequest" in full["components"]["schemas"]


# Golden values, shared with app/test/phoneapi.test.mjs: the app must make the same bytes.
GOLDEN = {
    "secret": bytes(range(16)),
    "token": bytes(range(100, 108)),
    "phone_d": 0x1F2E3D4C5B6A79881F2E3D4C5B6A79881F2E3D4C5B6A79881F2E3D4C5B6A7988,
    "base_d": 0x0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF,
}


def golden() -> dict:
    g = GOLDEN
    phone = ec.derive_private_key(g["phone_d"], ec.SECP256R1())
    base = ec.derive_private_key(g["base_d"], ec.SECP256R1())
    pp, bp = papi.public_bytes(phone), papi.public_bytes(base)
    key = papi.api_key(papi.ecdh(base, pp), g["secret"], g["token"], pp, bp)
    code = papi.make_code(g["secret"], g["token"], "a1b2c3d4", ["http://192.168.1.5:8100"])
    welcome = papi.seal(key, papi.welcome_aad(g["token"]), {"phone_id": "0000002a", "ble_key": "AAECAwQFBgcICQoLDA0ODw"},
                        iv=bytes(12))
    req = papi.seal(key, papi.request_aad("collars", "0000002a"), {"ts": 1700000000000, "body": {}}, iv=bytes([1] * 12))
    rep = papi.seal(key, papi.reply_aad("collars", "0000002a", bytes([1] * 12)), {"ok": True}, iv=bytes([2] * 12))
    return {"phone_pub": papi.b64e(pp), "base_pub": papi.b64e(bp), "proof": papi.b64e(papi.enroll_proof(g["secret"], g["token"], pp)),
            "api_key": key.hex(), "code": code, "welcome": welcome, "request": req, "reply": rep}


def test_golden_values_match_the_apps():
    g = golden()
    app_test = (Path(__file__).resolve().parents[2] / "app" / "test" / "phoneapi.test.mjs").read_text()
    for name in ("phone_pub", "base_pub", "proof", "api_key", "code"):
        assert g[name] in app_test, name
    for name in ("welcome", "request", "reply"):
        assert g[name]["ct"] in app_test, name
    assert papi.read_code(g["code"]) == {"secret": GOLDEN["secret"], "token": GOLDEN["token"], "home": "a1b2c3d4",
                                         "urls": ["http://192.168.1.5:8100"]}
