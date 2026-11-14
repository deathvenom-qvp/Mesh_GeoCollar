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

The easy way: plug the new collar into the base machine, open **Collar firmware**, pick **USB on
base**, type the dog's name, leave **Then set it up for this base** ticked and press **Flash** (or
**Set up only** if it already runs GeoCollar). The base reads the collar's node ID from its chip,
adds it, puts the base's public key in the collar's admin keys, and copies the base's LoRa
settings and channels to it. Flashing from **USB on this computer** also adds the collar, but its
Meshtastic settings then need the base's key by hand (below).

A collar running GeoCollar that no base has adopted says hello on the mesh every minute for 15
minutes after it starts, then every 15 minutes. If the base hears it (same LoRa settings and
primary channel), it shows under Collars as **New**, with an **Add** button.

By hand, collars only obey messages from base nodes they trust:

1. In the Meshtastic app, open the base node and copy its **public key** (Settings > Security).
2. On each collar, paste that key into **Admin key** (Settings > Security) and save.
3. In GeoCollar, open **Add a collar** and enter the collar's node ID (for example `!a1b2c3d4`,
   shown in the Meshtastic app's node list).

## If a dog gets lost

Give each collar the dog's name (**Rename** next to the collar's name) and fill in **If a dog
gets lost** with your name and phone number. The base sends both to every collar.

When a dog has been outside its fences for 90 seconds, its collar starts asking for help:

- It shows up on Bluetooth as "Rex is lost 5551234567" for 90 seconds, then goes quiet for 90
  seconds, and repeats. Anyone scanning with a phone (a Bluetooth scanner app, or the Meshtastic
  app's device list) sees the name.
- Every 5 minutes it posts a plain text message on the public Meshtastic channel: "Please help get
  Rex home. Call Ken 555-123-4567. Last seen 40.12345,-75.12345".
- It takes GPS fixes half as often as normal lost mode, so the battery lasts longer.

It stops when the dog is back inside, or when you press **Silence lost alert**. Silenced, it
stays quiet until the dog has been home and gets out again.

For this to work, keep **Bluetooth enabled** in the collar's Meshtastic settings (the collar
borrows Meshtastic's Bluetooth; with it disabled, nothing is advertised), and keep the default
public channel (LongFast with the default key) on the collar, at least as a secondary channel.
Without a public channel, the message goes on your primary channel and only your own nodes can
read it. Your phone number goes out in the clear to anyone nearby, which is the point.

## Drawing fences

To give a dog the same fences as another, pick the other collar in **Copy fences from another
collar** (in the Fences section), then **Send to collar**.


1. Select the collar. Switch the map to **Satellite** (top right) if it isn't already.
2. Press **+ Keep-in zone** and click each corner of the yard, then click the first corner or
   press **Finish**. Use **Rectangle** for a quick box.
3. Add **+ Keep-out zone** shapes inside it for places to avoid, such as a pond or a road edge.
4. Click a zone to drag its corners; drag a midpoint to add a corner, right-click a corner to
   remove it. Rename zones and switch their type in the side panel.
5. Set each zone's bands. The line you draw is the outer edge; going in from it:
   - **Danger** (red, optional): shock, if shock is enabled on the collar. Untick it and the line
     starts the warning band instead, so that zone never shocks.
   - **Warning** (yellow): vibrate.
   - **Attention** (light blue): beep.
   **Repeat beep** and **Repeat vibrate** set how often the collar repeats them while the dog stays
   in that band (every 2 to 10 s, or once per GPS fix). Once the dog is past the line the collar
   goes quiet, and stays quiet until the dog is back inside and clear of every band.
   The map shades each band so you can see exactly where it starts and ends. For a keep-out zone
   the bands lie outside its line.
6. Press **Send to collar**. The page shows when the collar reports the new fence version.

**GPS accuracy.** The collar's UC6580 receiver is rated at 1.5 m (RMS) in open sky. On a dog,
near buildings and under trees, expect 2 to 5 m, and the occasional jump of 10 m or more. Make
each band at least 3 m wide, and draw the line a few metres inside anything that really matters,
such as a road. The collar ignores fixes with poor satellite geometry.

Each collar holds up to 4 zones of up to 32 corners. The dog must be inside **every** keep-in
zone, so use one keep-in zone, not several side by side.

## Property lines

Most US counties publish parcel (tax map) lines on their GIS or property search site, and many let
you download a parcel as GeoJSON, KML or a shapefile. Under **Property lines**, press
**Import file…** and pick the file. For a shapefile, pick the `.zip`, or the `.shp`, `.dbf` and
`.prj` files together; the `.prj` tells the page how to convert the coordinates. Parcels show as
dashed orange outlines, saved on the base so every device sees them, and a zone you draw snaps
to their corners.

Click parcels on the map to select them; neighbouring lots you own merge into one outline. Then
set how far the perimeter sits back from the property line: **Same all round**, or **Each side**,
where the sides are numbered on the map and listed with the way they face and their length. A
negative set-back puts the perimeter outside the line. A dotted green line previews the
perimeter, and **Make keep-in zone** turns it into a zone for the selected collar; the zone's
danger, warning and attention bands sit inside it. Outlines with more than 32 corners are
simplified, and the page says by how much the line moved. Tax-map lines are often 1 to 5 m off,
so compare the zone with the satellite view before sending it, and keep the danger band well
inside anything that matters, such as a road. A whole-county file is cut down to the 500 parcels nearest the selected collar, or the middle of
the map if it has no position yet, so centre the map on your property before importing one.

## Trying it without a collar

Press **+ Simulated collar** under the collar list to add a pretend collar in the middle of the
map. Draw and send fences to it as usual, then use the **Simulation** panel: **Place on map**
moves it, **Wander** lets it roam, **Walk to point** sends it to a spot you click, and the speed,
report rate and GPS error are adjustable. It applies the same fence rules as the collar
firmware, and **What the collar did** lists each beep, vibration, shock and switch to lost mode.
Commands such as Lost mode work on it too. Remove it like any other collar.

## Updating collar firmware

Open **Collar firmware** at the bottom of the side panel.

1. **Get a build.** *Download latest build* fetches the newest build of the main branch, which
   GitHub Actions publishes as the `firmware-latest` release. *Build from source here* compiles
   it on this machine instead: the first build downloads about 1.5 GB of tools and can take
   30 minutes or more on a Raspberry Pi. The last three builds are kept.
2. **Flash it** one of three ways:
   - **USB on base**: plug the collar into the Home Assistant machine and pick its port. The
     base station's own radio port is greyed out. If the collar's port is missing, restart the app
     after plugging it in.
   - **USB on this computer**: plug the collar into the computer you are browsing from. Needs
     Chrome or Edge, and Home Assistant opened over `https://`.
   - **Wi-Fi**: select the collar, save your Wi-Fi network once, and press *Update over Wi-Fi*.
     The base sends the Wi-Fi details to the collar over the mesh (they stay saved, with the
     collar's own Wi-Fi left off to save battery), restarts it into Meshtastic's update loader,
     finds it on your network and sends the firmware. Put the collar on its charger in Wi-Fi
     range first: **it does not enforce fences while it updates**, usually 2 to 5 minutes.

The first install on a new collar must be over USB, as a **Full install**: that also puts
Meshtastic's update loader on the collar, which Wi-Fi updates need. *Update, keep settings* keeps
the collar's keys and fences; a full install erases them, so pair the collar again afterwards.

If a Wi-Fi update fails partway, the collar waits in update mode, and the page says so. Press
*Retry upload*, or flash it over USB, which also brings it out of update mode. If the base can't
find the collar on the network, enter the collar's address (from your router's device list), or
set **Network to search for collars being updated** on the Configuration tab, such as
`192.168.1.0/24`.

## Data and access

Collars, fences and recent positions are kept in the app's `/data` folder and included in Home
Assistant backups. Through the sidebar the page is behind your Home Assistant login. The
optional port on the **Network** tab opens the page without any login, so leave it empty unless
you need it and trust your network.
