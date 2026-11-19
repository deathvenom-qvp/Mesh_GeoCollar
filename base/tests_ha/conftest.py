# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests of the Home Assistant integration in a real Home Assistant (pytest-homeassistant-custom-component).
Home Assistant finds it as custom_components/geocollar, which a temporary folder links to."""

import sys
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(tempfile.mkdtemp(prefix="geocollar-ha-"))
(_ROOT / "custom_components").mkdir()
(_ROOT / "custom_components" / "__init__.py").write_text("")
(_ROOT / "custom_components" / "geocollar").symlink_to(
    Path(__file__).resolve().parents[1] / "geocollar_base" / "ha_integration" / "geocollar")
sys.path.insert(0, str(_ROOT))


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def pytest_configure(config):
    import custom_components  # noqa: F401 - before Home Assistant's test plugin looks for it
