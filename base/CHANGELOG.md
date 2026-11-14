# Changelog

## 0.5.0

- Band colours: attention light blue, warning yellow, danger red.
- Repeat beep and vibrate every 2 to 10 seconds while the dog stays in a band.
- Past the line the collar stops all corrections, and stays quiet until the dog is back inside
  and clear of every band. Needs the matching collar firmware.

## 0.4.0

- Each zone now has three bands inside its line: danger (shock), warning (vibrate) and attention
  (beep), each with its own width. The danger band can be turned off per zone, so that zone never
  shocks. The map shades each band in its own colour.
- Needs collar firmware with protocol v2; older collars ignore the new messages. Fences saved
  before this version are converted (their warning distance becomes warning + attention).
- GPS accuracy note and checks for bands narrower than the GPS error or covering the whole zone.

## 0.3.0

- Simulated collars for trying fences without hardware: place one on the map, let it stay,
  wander or walk to a point at a chosen speed, with GPS error. It follows the collar firmware's
  fence rules and lists the beeps, vibrations, shocks and lost-mode changes it would make.

## 0.2.2

- Fix the street map showing "Access blocked" inside Home Assistant: send a referrer with
  map tile requests, which OpenStreetMap requires.

## 0.2.1

- Fix the Home Assistant build: always build on python:3.12-slim instead of Home Assistant's
  default Alpine base, which has no Python.

## 0.2.0

- Draw, edit and delete keep-in and keep-out zones on a satellite or street map, with checks
  against the collar's limits before sending.
- Every collar's last position and the selected collar's recent track on the map.
- Reconnects to the Meshtastic node automatically, and shows why it is not connected.
- Home Assistant: sidebar panel, option descriptions, watchdog, icon and documentation.

## 0.1.0

- First skeleton: collar list, basic fence drawing, manual commands.
