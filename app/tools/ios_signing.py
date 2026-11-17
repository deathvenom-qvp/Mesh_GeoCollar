#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Sets up the iOS project for a signed build on GitHub (see .github/workflows/app.yml).

    python3 tools/ios_signing.py PROFILE.plist IDENTITY OUT_DIR

PROFILE.plist is the provisioning profile, decoded (`security cms -D -i x.mobileprovision`).
IDENTITY is the signing certificate's name as the keychain shows it ("Apple Distribution: ...").

It reads the team, the bundle identifier and the kind of profile from the profile itself, so the
only things to set up are the certificate and the profile. Then it:

- switches the App target to manual signing with that profile and certificate, and the profile's
  bundle identifier (only the App target: the Swift packages it uses stay unsigned);
- writes OUT_DIR/ExportOptions.plist for `xcodebuild -exportArchive`, with the export method
  that matches the profile (development, ad hoc, App Store or enterprise);
- prints PROFILE_UUID, TEAM_ID, BUNDLE_ID and EXPORT_METHOD as KEY=value lines, for $GITHUB_ENV.
"""

import os
import plistlib
import sys

PBXPROJ = os.path.join(os.path.dirname(__file__), '..', 'ios', 'App', 'App.xcodeproj', 'project.pbxproj')


def export_method(profile):
    """The exportArchive method (Xcode 15.3+ names) that a profile of this kind is for."""
    if profile.get('ProvisionsAllDevices'):
        return 'enterprise'
    if profile.get('ProvisionedDevices'):
        # A development profile lets a debugger attach; an ad hoc one does not.
        return 'debugging' if profile.get('Entitlements', {}).get('get-task-allow') else 'release-testing'
    return 'app-store-connect'


def bundle_id(profile, team, current):
    """The bundle identifier the profile is for, or the project's own if it is a wildcard profile."""
    app_id = profile['Entitlements']['application-identifier']
    prefix = profile.get('ApplicationIdentifierPrefix', [team])[0] + '.'
    if app_id.startswith(prefix):
        app_id = app_id[len(prefix):]
    return current if app_id.endswith('*') else app_id


def quote(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def main(profile_path, identity, out_dir):
    with open(profile_path, 'rb') as f:
        profile = plistlib.load(f)
    uuid = profile['UUID']
    team = profile['TeamIdentifier'][0]
    method = export_method(profile)

    with open(PBXPROJ) as f:
        project = f.read()
    # The App target's Debug and Release configurations are the only ones with a signing style.
    automatic = 'CODE_SIGN_STYLE = Automatic;'
    if project.count(automatic) != 2:
        sys.exit(f'expected the App target\'s two "{automatic}" lines in {PBXPROJ}, found {project.count(automatic)}')
    current_ids = {line.split('=', 1)[1].strip(' ;"') for line in project.splitlines()
                   if line.strip().startswith('PRODUCT_BUNDLE_IDENTIFIER =')}
    if len(current_ids) != 1:
        sys.exit(f'expected one bundle identifier in {PBXPROJ}, found {sorted(current_ids)}')
    current = current_ids.pop()
    bundle = bundle_id(profile, team, current)

    indent = '\n\t\t\t\t'
    project = project.replace(automatic, indent.join([
        'CODE_SIGN_STYLE = Manual;',
        f'CODE_SIGN_IDENTITY = {quote(identity)};',
        f'DEVELOPMENT_TEAM = {quote(team)};',
        f'PROVISIONING_PROFILE_SPECIFIER = {quote(uuid)};',
    ]))
    project = project.replace(f'PRODUCT_BUNDLE_IDENTIFIER = {current};', f'PRODUCT_BUNDLE_IDENTIFIER = {quote(bundle)};')
    with open(PBXPROJ, 'w') as f:
        f.write(project)

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, 'ExportOptions.plist'), 'wb') as f:
        plistlib.dump({
            'method': method,
            'teamID': team,
            'signingStyle': 'manual',
            'signingCertificate': identity,
            'provisioningProfiles': {bundle: uuid},
        }, f)

    print(f'PROFILE_UUID={uuid}')
    print(f'TEAM_ID={team}')
    print(f'BUNDLE_ID={bundle}')
    print(f'EXPORT_METHOD={method}')


if __name__ == '__main__':
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(*sys.argv[1:])
