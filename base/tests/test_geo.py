# SPDX-License-Identifier: GPL-3.0-or-later
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from geocollar_base import geo
from geocollar_base.app import create_app

NOMINATIM_REPLY = [
    {"display_name": "1600 Pennsylvania Avenue NW, Washington, DC", "lat": "38.8976", "lon": "-77.0366",
     "boundingbox": ["38.8971", "38.8981", "-77.0371", "-77.0361"], "addresstype": "building"},
    {"display_name": "broken"},
]


def test_geocoder_parses_caches_and_paces(monkeypatch):
    urls = []
    g = geo.Geocoder(fetch=lambda url: urls.append(url) or NOMINATIM_REPLY)
    sleeps = []
    monkeypatch.setattr(geo.time, "sleep", sleeps.append)
    r = g.search("  1600 Pennsylvania   Ave ", viewbox=(-77.1, 38.8, -77.0, 38.9))
    assert r == [{"name": "1600 Pennsylvania Avenue NW, Washington, DC", "lat": 38.8976, "lon": -77.0366,
                  "bbox": [38.8971, 38.8981, -77.0371, -77.0361], "kind": "building"}]
    q = parse_qs(urlparse(urls[0]).query)
    assert q["q"] == ["1600 Pennsylvania Ave"] and q["format"] == ["jsonv2"] and q["viewbox"] == ["-77.1,38.8,-77.0,38.9"]
    assert g.search("1600 pennsylvania ave", viewbox=(-77.1, 38.8, -77.0, 38.9)) == r and len(urls) == 1  # cached
    g.search("somewhere else")
    assert len(urls) == 2 and sleeps  # waited to keep to one request a second
    with pytest.raises(geo.GeoError, match="3 characters"):
        g.search("ab")


def test_geocoder_offline():
    def down(url):
        raise OSError("no route")

    with pytest.raises(geo.GeoError, match="not reachable"):
        geo.Geocoder(fetch=down).search("Main Street")


def test_home_location():
    assert geo.ha_home(fetch=lambda url: {"latitude": 40.1, "longitude": -75.2, "location_name": "Farm"}) == {
        "lat": 40.1, "lon": -75.2, "name": "Farm", "source": "home_assistant"}
    assert geo.ha_home(fetch=lambda url: {"latitude": 0, "longitude": 0}) is None  # never set
    assert geo.ha_home(fetch=lambda url: {}) is None
    assert geo.parse_home("40.5, -75.25")["lat"] == 40.5
    assert geo.parse_home("") is None and geo.parse_home("north") is None and geo.parse_home("91,0") is None


def test_map_api(tmp_path, monkeypatch):
    app = create_app({"data_dir": str(tmp_path), "home": "40.5, -75.25"}, link=type("L", (), {"connected": False, "close": lambda s: None})())
    c = TestClient(app)
    assert c.get("/api/home").json()["source"] == "options"
    app.state.geocoder._fetch = lambda url: NOMINATIM_REPLY
    r = c.get("/api/geocode", params={"q": "1600 Pennsylvania"}).json()
    assert r["results"][0]["lat"] == 38.8976
    assert c.get("/api/geocode", params={"q": "ab"}).status_code == 400
