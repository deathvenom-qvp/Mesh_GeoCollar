# SPDX-License-Identifier: GPL-3.0-or-later
"""Constants for the GeoCollar integration."""

DOMAIN = "geocollar"
# The phone app reaches the base at <Home Assistant's URL> + PATH + /api/phone/v1/...
PATH = "/api/geocollar/phone"
PHONE_API = "api/phone/v1/"  # the only paths passed on
MAX_BODY = 64 * 1024  # the base's own limit
CONF_PHONE_URL = "phone_url"  # where the base's phone API listens, e.g. http://xxxx-geocollar-base:8100
CONF_PAGE_URL = "page_url"  # the base's page, told which addresses Home Assistant has
