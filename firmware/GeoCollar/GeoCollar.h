// SPDX-License-Identifier: GPL-3.0-or-later
//
// GeoCollar: an optional Meshtastic module that keeps a dog inside geofences using a
// CaiXianLin-type training collar, and helps find the dog over the mesh if it gets out.
//
// Built on Meshtastic (https://meshtastic.org, GPL-3.0). Meshtastic picks up any module dropped
// into src/modules/optional/<Name>/ with a <Name>.h that declares setup<Name>(); see
// bin/optional-modules.py in the Meshtastic firmware.

#pragma once

/// Called by Meshtastic's optional-module loader during setupModules().
void setupGeoCollar();
