# Changelog

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
