# SPDX-License-Identifier: GPL-3.0-or-later
"""The GeoCollar integration: set up from the base app's discovery, passing the phone API on at
Home Assistant's own address, and telling the base that address."""

from aiohttp import web
from homeassistant.config_entries import SOURCE_HASSIO, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from homeassistant.setup import async_setup_component

DOMAIN = "geocollar"


async def fake_base(aiohttp_server):
    """The base app's two ports in one: the phone API and the page's /api/ha-link."""
    seen = {"posts": [], "links": []}

    async def phone(request):
        body = await request.json()
        seen["posts"].append((request.path, body))
        if body.get("phone") == "unknown":
            return web.json_response({"detail": "no", "code": "unknown_phone"}, status=401)
        return web.json_response({"iv": "AAAA", "ct": "sealed:" + body.get("ct", "")})

    async def link(request):
        seen["links"].append(await request.json())
        return web.json_response({"urls": []})

    app = web.Application()
    app.router.add_post("/api/phone/v1/{op}", phone)
    async def spec(request):
        return web.json_response({"openapi": "3.1.0"})

    async def page(request):
        return web.Response(text="the page: must never be reachable")

    app.router.add_get("/api/phone/v1/openapi.json", spec)
    app.router.add_get("/", page)
    app.router.add_put("/api/ha-link", link)
    server = await aiohttp_server(app)
    return server, seen


async def discover(hass, server):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_HASSIO}, data=HassioServiceInfo(
        config={"host": "127.0.0.1", "phone_port": server.port, "page_port": server.port},
        name="GeoCollar base", slug="geocollar_base", uuid="1234"))


async def test_discovered_set_up_and_passing_on(hass: HomeAssistant, hass_client_no_auth, aiohttp_server):
    await async_setup_component(hass, "http", {})
    hass.config.internal_url = "http://192.168.1.5:8123"
    hass.config.external_url = "https://ha.example.com"
    server, seen = await fake_base(aiohttp_server)

    # Discovered: Home Assistant asks once, then it is set up.
    r = await discover(hass, server)
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "hassio_confirm"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert r["data"] == {"phone_url": f"http://127.0.0.1:{server.port}", "page_url": f"http://127.0.0.1:{server.port}"}
    await hass.async_block_till_done()
    # The base is told Home Assistant's addresses with the path.
    assert seen["links"][-1] == {"external": "https://ha.example.com/api/geocollar/phone",
                                 "internal": "http://192.168.1.5:8123/api/geocollar/phone"}
    # Discovered again (the app restarted): no second entry.
    r = await discover(hass, server)
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "already_configured"

    # The phone app's requests go through without a Home Assistant login, as they are.
    client = await hass_client_no_auth()
    resp = await client.post("/api/geocollar/phone/api/phone/v1/collars", json={"phone": "0000002a", "iv": "x", "ct": "y"})
    assert resp.status == 200 and (await resp.json()) == {"iv": "AAAA", "ct": "sealed:y"}
    assert seen["posts"][-1] == ("/api/phone/v1/collars", {"phone": "0000002a", "iv": "x", "ct": "y"})
    # The base's errors come back as they are.
    resp = await client.post("/api/geocollar/phone/api/phone/v1/collars", json={"phone": "unknown"})
    assert resp.status == 401 and (await resp.json())["code"] == "unknown_phone"
    resp = await client.get("/api/geocollar/phone/api/phone/v1/openapi.json")
    assert resp.status == 200
    # Nothing but the phone API: not the page, not a way around the path.
    for path in ("/api/geocollar/phone/", "/api/geocollar/phone/api/ha-link", "/api/geocollar/phone/api/phone/v1/../../"):
        resp = await client.get(path)
        assert resp.status == 404, path
    resp = await client.post("/api/geocollar/phone/api/phone/v1/collars", data=b"x" * (64 * 1024 + 1),
                             headers={"Content-Type": "application/json"})
    assert resp.status == 413

    # Unloaded: the path stops passing anything on.
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert await hass.config_entries.async_unload(entry.entry_id)
    resp = await client.post("/api/geocollar/phone/api/phone/v1/collars", json={})
    assert resp.status == 404


async def test_base_not_running(hass: HomeAssistant, hass_client_no_auth):
    await async_setup_component(hass, "http", {})
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"phone_url": "nonsense", "page_url": ""})
    assert r["errors"] == {"phone_url": "bad_url"}
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"phone_url": "http://127.0.0.1:9/x", "page_url": ""})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert r["data"] == {"phone_url": "http://127.0.0.1:9", "page_url": "http://127.0.0.1:8099"}
    await hass.async_block_till_done()
    client = await hass_client_no_auth()
    resp = await client.post("/api/geocollar/phone/api/phone/v1/collars", json={})
    assert resp.status == 502 and (await resp.json())["code"] == "offline"
