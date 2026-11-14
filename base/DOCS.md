# GeoCollar base

Draw geofences for your Meshtastic dog collars, send them over the mesh, and watch where each
dog is. The page opens from the **GeoCollar** entry in the Home Assistant sidebar.

## What you need

- A Meshtastic node for the base station (any board running stock Meshtastic), either plugged
  into the Home Assistant machine by USB or on your network over Wi-Fi or Ethernet.
- One or more collars running the GeoCollar firmware, on the same Meshtastic channel settings
  as the base node.

## Setting up the base radio

The base radio is an ordinary Meshtastic node running stock Meshtastic 2.5 or later (2.8 is
best): a Heltec V3, a LilyGO T-Beam, a RAK WisBlock, a Station G2 and so on. It does not run
GeoCollar firmware. Before connecting it to this app:

1. Flash stock Meshtastic with the [Meshtastic web flasher](https://flasher.meshtastic.org).
2. In the Meshtastic phone app (or [client.meshtastic.org](https://client.meshtastic.org)), set the
   **LoRa region** for your country. Leave the preset at the default (LongFast) unless you have a
   reason to change it; collars get the same settings.
3. Leave the channels at the default. You can change them later from this app (see below), which
   also moves the collars.
4. Put the antenna as high as you can. The base's range is the yard you can cover.

**USB or network?** USB is the better choice: plug the radio into the Home Assistant machine with
a data cable. It is powered, needs no Wi-Fi setup, and keeps working if Wi-Fi drops. Use the
network instead when the Home Assistant machine sits somewhere with poor radio range (a basement or
a rack): put the radio by a window or in the attic on Wi-Fi or Ethernet (port 4403, Meshtastic's
network API, which is on whenever Wi-Fi or Ethernet is enabled). On ESP32 boards, turning on Wi-Fi
turns off Bluetooth, so you configure the node over the network from then on.

Once connected, click the radio badge at the top of the page (or **Base radio** under it) to see
the radio's node, firmware, region, preset and channel, and any problem that would stop collars
working, such as no region set or firmware too old.

### Changing the base radio's settings

Open **Base radio** and choose **Change settings…** to change the radio's name, region, preset,
hop limit or primary channel. The base and every collar must share the region, preset and
channels, or they stop hearing each other, and a collar locked to this base can only be changed by
the base over the mesh. So when one of those changes, the app:

1. asks every collar for a reply, and changes nothing if one doesn't answer (bring it in range, or
   tick **Change anyway** to leave it behind);
2. sends each collar the new settings over the mesh, and it restarts on them;
3. changes the base radio last. It restarts too, and the app reconnects within a minute.

Each moved collar shows "Waiting for its first report" until it is heard on the new settings. A
collar left behind can't hear the base: plug it into the base or this computer and use **Set up
only** under Collar firmware
(after a full install, if it is locked to this base). The name and hop limit only affect the base.

If the base radio fails to take the settings after the collars have moved, the panel shows
**Finish the change**, which applies the same settings (including any new key) to the base.

**Channels.** The primary channel can stay as it is, go back to the public default (anyone nearby
can read it), or get a new private key. Tick **Keep a public LongFast channel** so lost-dog
messages still reach strangers' radios. Leave a private channel's name empty: radios pick their
frequency from the primary channel's name, and a blank name keeps the default LongFast frequency,
which is the one strangers listen on. With a named channel, lost-dog messages reach only radios
set up like yours.

## Configuration

Set **one** of these on the **Configuration** tab, then restart the app:

| Option | Use it when | Example |
| --- | --- | --- |
| Meshtastic node (USB) | the node is plugged into this machine | `/dev/serial/by-id/usb-Silicon_Labs_CP2102_...` |
| Meshtastic node (network) | the node is on Wi-Fi or Ethernet | `192.168.1.50` |

Pick the USB device from the list rather than typing `/dev/ttyUSB0`: the `/dev/serial/by-id/`
names stay the same after a reboot. A network node must have Meshtastic's network API on
(port 4403, the default when Wi-Fi is enabled).

Only one program can use a node at a time, so don't point another Meshtastic tool or
integration (including Home Assistant's own Meshtastic integration) at the same node while this
app runs. Over the network, a Meshtastic node also serves only one client at a time. If the node is unplugged or rebooted, the
app keeps retrying every 30 seconds; the badge at the top of the page shows whether the radio is
connected, and hovering it shows the last error.

## Pairing a collar

The easy way: plug the new collar into the base machine, open **Collar firmware**, pick **USB on
base**, type the dog's name, leave **Then set it up for this base** ticked and press **Flash** (or
**Set up only** if it already runs GeoCollar). The base reads the collar's node ID from its chip,
adds it, puts the base's public key in the collar's admin keys, and copies the base's LoRa
settings and channels to it.

**From the computer you're using** (Chrome or Edge, with the page opened over HTTPS): plug the
collar into this computer, pick **USB on this computer**, and press **Connect and flash** (with
**Then set it up for this base** ticked) or **Set up only**. The setup is the same as on the
base's USB: the page passes the collar's serial port through to the base app, which does the
work. After flashing, the page finds the collar again when it restarts; if it can't, press **Set
up only** and pick the collar.

A collar running GeoCollar that no base has adopted says hello on the mesh every minute for 15
minutes after it starts, then every 15 minutes. If the base hears it (same LoRa settings and
primary channel), it shows under Collars as **New**, with an **Add** button. A collar on the base
that says hello hasn't heard from the base yet; the base answers it, and it starts reporting.

**How to tell it runs GeoCollar.** A freshly installed collar names itself **GeoCollar** and the
last four characters of its node ID (for example "GeoCollar c3d4") until the base names it after
the dog; otherwise it looks like stock Meshtastic (same version number) in the Meshtastic app. Its
serial log says "GeoCollar: ready" at start. A fresh install has no LoRa region, so it can't
transmit anything until it is set up: use **Set up only** (or set the region and the base's key by
hand). Until a collar reports, its panel says what it is waiting for.

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

## Finding your place on the map

Type an address in the search box at the top right of the map and press Enter. The map starts
at Home Assistant's home location (Settings > System > General) and the ⌂ button returns there;
the **Home location** option overrides it. Searches go to OpenStreetMap's Nominatim service, so
the base needs internet access for them.

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

## Security: who can control a collar

- A collar obeys GeoCollar messages only when they are PKI-encrypted direct messages from a
  public key in its admin keys, which is your base's key after setup. Another base, even one set
  up exactly like yours, has a different key and is ignored. Meshtastic's own remote settings
  follow the same rule.
- **Lock it to this base** (on by default when you set a collar up over USB) turns on Meshtastic's
  managed mode. The collar then refuses settings changes from Bluetooth, USB and the network
  too, so only your base can change it. The only way round it is a full install over USB, which
  erases the collar, keys and all. Pick your base's channels before setting collars up; a locked
  collar keeps the ones it was given until it is reinstalled. Locking also gives the collar a
  random Bluetooth PIN, shown in its panel.
- Every command carries a sequence number and every fence set a version, and the collar refuses
  old ones, so recorded radio packets cannot be played back to it.
- The base accepts reports only from collars you added, only PKI-encrypted, and only from the
  public key it first saw from that collar. A report signed with another key is ignored and
  flagged in the collar's panel. After a full install of a collar, set it up again (or remove and
  re-add it) so the base learns its new key.
- During a Wi-Fi update, Meshtastic's update loader waits on your home network for the new image
  for a few minutes. Update collars on a network you trust.

## Data and access

Collars, fences and recent positions are kept in the app's `/data` folder and included in Home
Assistant backups. Through the sidebar the page is behind your Home Assistant login. The
optional port on the **Network** tab opens the page without any login, so leave it empty unless
you need it and trust your network.
