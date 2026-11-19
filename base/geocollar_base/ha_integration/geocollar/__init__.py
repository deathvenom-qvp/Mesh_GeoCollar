# SPDX-License-Identifier: GPL-3.0-or-later
"""GeoCollar: lets the GeoCollar phone app reach the GeoCollar base app through Home Assistant's
own address (your reverse proxy's HTTPS URL, or Home Assistant Cloud), with no port or proxy
host of its own.

It adds one path, /api/geocollar/phone/..., and passes requests under it to the base app's phone
API. That path needs no Home Assistant login: the phone app has none. It does not need one,
because every request on it is encrypted and signed with a key of the phone's own, which only the
base can check; anything else is refused by the base. Only the phone API is passed on, never the
base's page, and nothing here can read what passes through.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import aiohttp
from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import CONF_PAGE_URL, CONF_PHONE_URL, DOMAIN, MAX_BODY, PATH, PHONE_API

_LOGGER = logging.getLogger(__name__)
TELL_EVERY = timedelta(hours=1)


class PhoneApiView(HomeAssistantView):
    """Passes the phone app's requests on to the base app."""

    url = PATH + "/{path:.*}"
    name = "api:geocollar:phone"
    requires_auth = False  # the phone API checks every request itself (see above)
    cors_allowed = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    def _target(self) -> str | None:
        return self.hass.data.get(DOMAIN, {}).get(CONF_PHONE_URL)

    async def _forward(self, request: web.Request, path: str) -> web.Response:
        target = self._target()
        if not target or not path.startswith(PHONE_API) or ".." in path:
            return web.json_response({"detail": "Not found", "code": "not_found"}, status=404)
        body = b""
        if request.method == "POST":
            if (request.content_length or 0) > MAX_BODY:
                return web.json_response({"detail": "The request is too large.", "code": "too_large"}, status=413)
            buf = bytearray()
            async for chunk in request.content.iter_chunked(8192):
                buf += chunk
                if len(buf) > MAX_BODY:
                    return web.json_response({"detail": "The request is too large.", "code": "too_large"}, status=413)
            body = bytes(buf)
        session = async_get_clientsession(self.hass)
        try:
            async with session.request(
                request.method, f"{target.rstrip('/')}/{path}", data=body or None,
                headers={"Content-Type": request.headers.get("Content-Type", "application/json")},
                timeout=aiohttp.ClientTimeout(total=20), allow_redirects=False,
            ) as r:
                data = await r.read()
                return web.Response(status=r.status, body=data,
                                    content_type=(r.content_type or "application/json"))
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            _LOGGER.warning("the GeoCollar base app did not answer at %s: %s", target, e)
            return web.json_response({"detail": "The GeoCollar base app is not running.", "code": "offline"}, status=502)

    async def post(self, request: web.Request, path: str) -> web.Response:
        return await self._forward(request, path)

    async def get(self, request: web.Request, path: str) -> web.Response:
        return await self._forward(request, path)  # the OpenAPI document


def _urls(hass: HomeAssistant) -> dict:
    """Home Assistant's addresses, with the path: from outside (your URL or the cloud's) and at home."""
    out = {}
    for key, kwargs in (("external", {"allow_internal": False, "prefer_cloud": True}),
                        ("internal", {"allow_external": False, "allow_cloud": False})):
        try:
            out[key] = get_url(hass, **kwargs).rstrip("/") + PATH
        except NoURLAvailableError:
            out[key] = None
    return out


async def _tell_base(hass: HomeAssistant, entry: ConfigEntry, *_args) -> None:
    """Tell the base app which addresses phones can reach it at through Home Assistant."""
    page = entry.data.get(CONF_PAGE_URL)
    if not page:
        return
    try:
        async with async_get_clientsession(hass).put(
            f"{page.rstrip('/')}/api/ha-link", json=_urls(hass), timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            if r.status != 200:
                _LOGGER.warning("the GeoCollar base app answered %s to the addresses", r.status)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        _LOGGER.info("could not tell the GeoCollar base app the addresses yet: %s", e)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = hass.data.setdefault(DOMAIN, {})
    data[CONF_PHONE_URL] = entry.data[CONF_PHONE_URL]
    if not data.get("view"):
        hass.http.register_view(PhoneApiView(hass))  # a view stays for as long as Home Assistant runs
        data["view"] = True
    await _tell_base(hass, entry)
    entry.async_on_unload(async_track_time_interval(hass, lambda now: hass.async_create_task(_tell_base(hass, entry)), TELL_EVERY))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.get(DOMAIN, {}).pop(CONF_PHONE_URL, None)  # the path then answers "not found"
    return True
