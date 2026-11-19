# SPDX-License-Identifier: GPL-3.0-or-later
"""Setting up GeoCollar: found by itself when the base app runs as a Home Assistant app, or by its
address when it runs in Docker."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from yarl import URL

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .const import CONF_PAGE_URL, CONF_PHONE_URL, DOMAIN


def _clean(url: str) -> str | None:
    try:
        u = URL(url.strip())
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.host:
        return None
    return str(u.with_path("").with_query(None).with_fragment(None)).rstrip("/")


class GeoCollarConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._found: dict[str, str] = {}

    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> ConfigFlowResult:
        """The base app announced itself through the Supervisor."""
        cfg = discovery_info.config
        host = cfg.get("host")
        if not host:
            return self.async_abort(reason="no_address")
        self._found = {
            CONF_PHONE_URL: f"http://{host}:{int(cfg.get('phone_port') or 8100)}",
            CONF_PAGE_URL: f"http://{host}:{int(cfg.get('page_port') or 8099)}",
        }
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured(updates=self._found)
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="GeoCollar", data=self._found)
        self._set_confirm_only()
        return self.async_show_form(step_id="hassio_confirm")

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """By hand: the base app in Docker somewhere Home Assistant can reach."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors = {}
        if user_input is not None:
            phone, page = _clean(user_input[CONF_PHONE_URL]), _clean(user_input.get(CONF_PAGE_URL) or "")
            if not phone:
                errors[CONF_PHONE_URL] = "bad_url"
            else:
                if not page:  # the page on the same machine, port 8099
                    page = str(URL(phone).with_port(8099)).rstrip("/")
                return self.async_create_entry(title="GeoCollar", data={CONF_PHONE_URL: phone, CONF_PAGE_URL: page})
        return self.async_show_form(step_id="user", errors=errors, data_schema=vol.Schema({
            vol.Required(CONF_PHONE_URL, default="http://192.168.1.5:8100"): str,
            vol.Optional(CONF_PAGE_URL, default=""): str,
        }))
