// SPDX-License-Identifier: GPL-3.0-or-later
// Builds the web part of the app into www/, which Capacitor wraps for Android and iOS.

import { build } from 'esbuild';
import { cp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';

// What ends up inside www/main.js, each with its licence, for www/licenses.txt.
const BUNDLED = ['leaflet', 'jsqr', '@capacitor/core', '@capacitor/app', '@capacitor/preferences', '@capacitor/filesystem',
  '@capacitor/share', '@capacitor-community/bluetooth-le'];

// Which build this is, for the screen and the debug log: the GitHub run number and commit when
// built by the "Phone app" workflow, else "local".
const env = process.env;
const BUILD = {
  number: env.GITHUB_RUN_NUMBER || '',
  commit: (env.GITHUB_SHA || '').slice(0, 7),
  branch: env.GITHUB_REF_NAME || '',
  date: new Date().toISOString().slice(0, 16).replace('T', ' ') + ' UTC',
};

await rm('www', { recursive: true, force: true });
await mkdir('www/vendor/leaflet', { recursive: true });
await build({
  entryPoints: ['src/main.js'],
  bundle: true,
  format: 'iife',
  target: ['es2020'],
  minify: true,
  legalComments: 'linked',
  outfile: 'www/main.js',
  define: { __GEOCOLLAR_BUILD__: JSON.stringify(BUILD) },
});
await cp('src/index.html', 'www/index.html');
await cp('src/style.css', 'www/style.css');
await cp('node_modules/leaflet/dist/leaflet.css', 'www/vendor/leaflet/leaflet.css');
await cp('node_modules/leaflet/dist/images', 'www/vendor/leaflet/images', { recursive: true });
let notices = 'GeoCollar phone app. GPL-3.0-or-later. Source: https://github.com/deathvenom-qvp/Mesh_GeoCollar\n\nIt includes:\n';
for (const name of BUNDLED) {
  const pkg = JSON.parse(await readFile(`node_modules/${name}/package.json`, 'utf8'));
  const licence = await readFile(`node_modules/${name}/LICENSE`, 'utf8');
  notices += `\n${'='.repeat(72)}\n${pkg.name} ${pkg.version} (${pkg.license})\n${'='.repeat(72)}\n\n${licence.trim()}\n`;
}
await writeFile('www/licenses.txt', notices);
console.log('built www/');
