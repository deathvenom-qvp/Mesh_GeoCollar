# GeoCollar base

Draw geofences for your Meshtastic dog collars, send them over the mesh, and watch where each
dog is. The page opens from the **GeoCollar** entry in the Home Assistant sidebar.

## What you need

- A Meshtastic node for the base station (any board running stock Meshtastic), either plugged
  into the Home Assistant machine by USB or on your network over Wi-Fi or Ethernet.
- One or more collars running the GeoCollar firmware, on the same Meshtastic channel settings
  as the base node.

## Configuration

Set **one** of these on the **Configuration** tab, then restart the app:

| Option | Use it when | Example |
| --- | --- | --- |
| Meshtastic node (USB) | the node is plugged into this machine | `/dev/serial/by-id/usb-Silicon_Labs_CP2102_...` |
| Meshtastic node (network) | the node is on Wi-Fi or Ethernet | `192.168.1.50` |

Pick the USB device from the list rather than typing `/dev/ttyUSB0`: the `/dev/serial/by-id/`
names stay the same after a reboot. A network node must have Meshtastic's network API on
(port 4403, the default when Wi-Fi is enabled).

Only one program can use a node's USB port at a time, so don't point another Meshtastic tool or
integration at the same USB node while this app runs. If the node is unplugged or rebooted, the
app keeps retrying every 30 seconds; the badge at the top of the page shows whether the radio is
connected, and hovering it shows the last error.

## Pairing a collar

Collars only obey messages from base nodes they trust:

1. In the Meshtastic app, open the base node and copy its **public key** (Settings > Security).
2. On each collar, paste that key into **Admin key** (Settings > Security) and save.
3. In GeoCollar, open **Add a collar** and enter the collar's node ID (for example `!a1b2c3d4`,
   shown in the Meshtastic app's node list).

## Drawing fences

1. Select the collar. Switch the map to **Satellite** (top right) if it isn't already.
2. Press **+ Keep-in zone** and click each corner of the yard, then click the first corner or
   press **Finish**. Use **Rectangle** for a quick box.
3. Add **+ Keep-out zone** shapes inside it for places to avoid, such as a pond or a road edge.
4. Click a zone to drag its corners; drag a midpoint to add a corner, right-click a corner to
   remove it. Rename zones and switch their type in the side panel.
5. Set each zone's bands. The line you draw is the outer edge; going in from it:
   - **Danger** (red, optional): shock, if shock is enabled on the collar. Untick it and the line
     starts the warning band instead, so that zone never shocks.
   - **Warning** (orange): vibrate.
   - **Attention** (yellow): beep.
   The map shades each band so you can see exactly where it starts and ends. For a keep-out zone
   the bands lie outside its line.
6. Press **Send to collar**. The page shows when the collar reports the new fence version.

**GPS accuracy.** The collar's UC6580 receiver is rated at 1.5 m (RMS) in open sky. On a dog,
near buildings and under trees, expect 2 to 5 m, and the occasional jump of 10 m or more. Make
each band at least 3 m wide, and draw the line a few metres inside anything that really matters,
such as a road. The collar ignores fixes with poor satellite geometry.

Each collar holds up to 4 zones of up to 32 corners. The dog must be inside **every** keep-in
zone, so use one keep-in zone, not several side by side.

## Trying it without a collar

Press **+ Simulated collar** under the collar list to add a pretend collar in the middle of the
map. Draw and send fences to it as usual, then use the **Simulation** panel: **Place on map**
moves it, **Wander** lets it roam, **Walk to point** sends it to a spot you click, and the speed,
report rate and GPS error are adjustable. It applies the same fence rules as the collar
firmware, and **What the collar did** lists each beep, vibration, shock and switch to lost mode.
Commands such as Lost mode work on it too. Remove it like any other collar.

## Data and access

Collars, fences and recent positions are kept in the app's `/data` folder and included in Home
Assistant backups. Through the sidebar the page is behind your Home Assistant login. The
optional port on the **Network** tab opens the page without any login, so leave it empty unless
you need it and trust your network.
