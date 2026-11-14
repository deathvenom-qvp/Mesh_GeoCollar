# GeoCollar base

Draw geofences for your Meshtastic dog collars, send them over the mesh, and watch where each
dog is. The page opens from the **GeoCollar** entry in the Home Assistant sidebar.

The page has two tabs. **Collars** is the everyday view: the map, the list of collars, the
selected collar with its commands, its fences, and property lines. **Settings** holds what you
set up once and seldom touch: the base radio, adding a collar, your contact details for a lost
dog, and collar firmware.

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

Once connected, click the radio badge at the top of the page (or open the **Settings** tab) to
see, under **Base radio**, the radio's node, firmware, region, preset and channel, and any problem
that would stop collars working, such as no region set or firmware too old. A problem with the
radio also shows at the top of the Collars tab.

### Changing the base radio's settings

On the **Settings** tab, under **Base radio**, choose **Change settings…** to change the radio's name, region, preset,
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

The easy way: plug the new collar into the base machine, open the **Settings** tab and, under
**Collar firmware**, pick **USB on base**, type the dog's name, leave **Then set it up for this base** ticked and press **Flash firmware** (or
**Set up only** if it already runs GeoCollar). The base learns the collar's node ID from it,
adds it, puts the base's public key in the collar's admin keys, and copies the base's LoRa
settings and channels to it.

**From the computer you're using** (Chrome or Edge, with the page opened over HTTPS): plug the
collar into this computer, pick **USB on this computer**, and press **Connect and flash** (with
**Then set it up for this base** ticked) or **Set up only**. The setup is the same as on the
base's USB: the page passes the collar's serial port through to the base app, which does the
work.

**After flashing from the browser, the collar has to restart**, and often it does not do so by
itself: a collar that was put into download mode with its buttons stays there until someone
presses **RST**. The page does not assume either way. It shows a panel and waits until the collar
really answers, then starts the setup by itself. If nothing happens within about 10 seconds,
press the collar's RST button once (or unplug it and plug it back in). Some browsers then treat
the restarted collar as a new device that the page may not use: press **Continue setup** and
pick the collar in the browser's list. The same can happen when the collar restarts during the
setup; the page then shows a **Reconnect** button. **Skip the setup** leaves the collar flashed
but not set up; press **Set up only** later.

On the base's own USB the app waits for the collar the same way and, if the flashing tool could
not restart it, asks you to press RST. A collar that comes back under another port name is
followed there.

Plug in one collar at a time. The page leaves alone every other device it was allowed to use
before, and the base stops the setup if a different device answers after a restart, but with two
blank collars on one computer neither can tell which is which.

A collar running GeoCollar that no base has adopted says hello on the mesh every minute for 15
minutes after it starts, then every 15 minutes. If the base hears it (same LoRa settings and
primary channel), it shows on the Collars tab as **New**, with an **Add** button. A collar on the base
that says hello hasn't heard from the base yet; the base answers it, and it starts reporting.

**How to tell it runs GeoCollar.** A freshly installed collar names itself **GeoCollar** and the
last four characters of its node ID (for example "GeoCollar c3d4") until the base names it after
the dog; otherwise it looks like stock Meshtastic (same version number) in the Meshtastic app. Its
serial log says "GeoCollar: ready" at start. A fresh install has no LoRa region, so it can't
transmit anything until it is set up: use **Set up only** (or set the region and the base's key by
hand). Until a collar reports, its panel says what it is waiting for.

Setting the region on a fresh collar makes its key, and Meshtastic 2.8 then changes its node ID to
match; the setup follows it and keeps the collar's name and fences. The collar restarts twice
during setup (once for Bluetooth, once for the base's key and the lock), so it takes about a
minute. The setup ends by reconnecting and checking that its settings stuck.

Setup also gives the base's radio the collar's public key. The radio can only send the collar
encrypted messages once it has that key, and it is marked as verified, so a NodeInfo from another
node can't replace it; the base also accepts reports only when they carry that key. **Set up only**
on a collar that is already locked to this base does just this part. A collar set up by hand gets
its key to the base over the air: when it says hello and the base's radio doesn't know its key, the
base swaps NodeInfo with it. If the base's messages to a collar fail, its panel says why.

By hand, collars only obey messages from base nodes they trust:

1. In the Meshtastic app, open the base node and copy its **public key** (Settings > Security).
2. On each collar, paste that key into **Admin key** (Settings > Security) and save.
3. In GeoCollar, on the **Settings** tab under **Add a collar**, enter the collar's node ID (for
   example `!a1b2c3d4`, shown in the Meshtastic app's node list).

## Name and report rate

**Rename** next to a collar's name sets the dog's name and how often the collar reports its
position, from every 30 seconds to every hour (10 minutes unless you change it). More often uses
more battery and airtime. While a dog is lost, its collar reports every 30 seconds whatever the
setting. The collar also takes the dog's name as its Meshtastic name ("Rex", short name "REX"), so
it shows as the dog in the Meshtastic app and on other nodes. If the collar is out of reach, it
gets the change when it next reports. Both need collar firmware from 0.15.0 on; older collars keep
reporting every 10 minutes and keep their Meshtastic name.

## When a collar has no GPS fix

Indoors, under a metal roof or in a car a collar can lose its GPS fix. It then cannot tell where
the dog is, so it cannot correct. From collar firmware 0.16.0 on it says so: the collar shows
**no GPS fix** in the list, its marker turns grey and stays at the last position it had, and its
panel says "No fix now". The trail on the map only gets points from real fixes. The collar tells
the base when the fix goes and when it comes back (at most one such report in 5 minutes), and
reports within about 15 seconds of being switched on.

While a collar with fences has no fix it keeps its GPS searching all the time, so the fence works
again within about a minute of the dog coming back under open sky. Stock Meshtastic would give up
after 15 minutes and then look again only every 15 minutes to an hour. On the Heltec Wireless
Tracker this costs no extra battery, because that board's GPS is powered whenever the collar is on
(see Battery below).

The collar only ever uses fixes from its own GPS. A position shared with it by a phone running
the Meshtastic app, or a fixed position set in its Meshtastic settings, is ignored.

## Battery

What uses the collar's battery: the processor and Bluetooth, the GPS, the radio listening for the
mesh, and the screen's backlight while it is lit.

- **Screen.** The collar's screen turns off 30 seconds after it starts or its button is pressed.
  Meshtastic's own default is 10 minutes, and every text message on the mesh lights it again, so
  on a busy public channel the backlight could be on most of the time. A timeout you set yourself
  in the Meshtastic app is kept.
- **Reports.** Each report is a radio transmission. Every 10 minutes is a good default; every 30
  seconds costs noticeably more.
- **Lost alert.** While a dog is lost the collar reports every 30 seconds and advertises on
  Bluetooth, which drains it faster. Silence the alert once you have the dog.
- **GPS.** Deep inside its fences a collar takes a fix about once a minute; near a line it checks
  every second. On the Heltec Wireless Tracker that saves little: the board has one power switch
  for the GPS and the screen together, Meshtastic leaves it on, and so the GPS chip runs whenever
  the collar is on. Meshtastic can be told to switch it (the Position setting **GPS EN GPIO**, 3 on the
  Tracker V1.1), which turns the GPS off between fixes while the dog is deep inside, and the
  screen with it. That is untested with GeoCollar: the GPS may need most of a minute to find
  itself again each time, so try it with the dog on a lead and watch how often fixes arrive
  before relying on it.

## If a dog gets lost

Give each collar the dog's name (**Rename** next to the collar's name) and, on the **Settings**
tab, fill in **If a dog gets lost** with your name and phone number. The base sends both to every collar.

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

Press **+ Simulated collar** (on the **Settings** tab, under Add a collar) to add a pretend
collar in the middle of the map; the page switches to the Collars tab and selects it. Draw and send fences to it as usual, then use the **Simulation** panel: **Place on map**
moves it, **Wander** lets it roam, **Walk to point** sends it to a spot you click, and the speed,
report rate and GPS error are adjustable. It applies the same fence rules as the collar
firmware, and **What the collar did** lists each beep, vibration, shock and switch to lost mode.
Commands such as Lost mode work on it too. Remove it like any other collar.

## Updating collar firmware

Open the **Settings** tab; **Collar firmware** is its last section.

There is nothing to download first. **Pressing the flash button gets the latest firmware by
itself** (the newest build of the main branch, which GitHub Actions publishes as the
`firmware-latest` release) and then flashes it. If the base can't reach GitHub, it uses the
newest build it already has and says so in the job's log.

**Check for firmware updates** only tells you whether there is newer firmware; it downloads and
flashes nothing. It shows the latest build and names the collars that run something older. A
collar that can be updated also says so in its panel, and the Settings tab gets a dot. The base
compares the collar firmware's source, not the commit, so a change to the base app alone does not
count as a collar update. It knows what a collar runs only if the collar was flashed from this
app.

**Check automatically** repeats the check daily, weekly (the default) or monthly, or never. When
an automatic check finds newer firmware for one of your collars, Home Assistant shows a
notification, once per new firmware.

**Flash a collar** over USB, one of two ways:

- **USB on base**: plug the collar into the Home Assistant machine, pick its port and press
  **Flash firmware**. The base station's own radio port is greyed out. If the collar's port is
  missing, restart the app after plugging it in.
- **USB on this computer**: plug the collar into the computer you are browsing from and press
  **Connect and flash**. Needs Chrome or Edge, and Home Assistant opened over `https://`. See
  [Pairing a collar](#pairing-a-collar) for what happens when the collar has to be reset by hand.

The first install on a new collar is a **Full install**. *Update, keep settings* keeps the
collar's keys and fences; a full install erases them, so pair the collar again afterwards.

**Advanced: flash another build** lists the last three builds kept on this machine. Pick one to
flash it instead of the latest. *Build from source here* compiles the firmware on this machine:
the first build downloads about 1.5 GB of tools and can take 30 minutes or more on a Raspberry Pi.

**There are no updates over Wi-Fi.** Versions before 0.17.0 had them, and kept your Wi-Fi
password on every collar updated that way.
The app no longer writes Meshtastic's update loader to a collar: a full install erases one left
by an older version, and after an update it stays on the collar but nothing starts it. If an
older version had saved your Wi-Fi network, the app forgets it when it starts (data file and
backup), and **Collar firmware** offers **Remove it from the collars** once: the base asks each
collar over the mesh and blanks the network on those that still hold it. Such a collar restarts
once. A collar that was out of range is asked again the next time you press the button. A collar
that an older version left in update mode says so in its panel; flash it over USB.

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
  old ones, so recorded radio packets cannot be played back to it. From collar firmware 0.16.0
  the dog's name, your contact details and the report rate are numbered the same way, and sending
  the fences a collar already has no longer restarts its shock limits or its lost alert.
- The collar's hardware switch (if you fitted one) turns off every correction, including a beep,
  vibration or shock you send from the page.
- The page has no login of its own. So that another website open in your browser cannot send
  commands to the base behind your back, the base refuses any change that comes from a page other
  than its own.
- The base accepts reports only from collars you added, only PKI-encrypted, and only from the
  public key it first saw from that collar. A report signed with another key is ignored and
  flagged in the collar's panel. After a full install of a collar, set it up again (or remove and
  re-add it) so the base learns its new key.
- Collar firmware can only be changed over USB, with the collar in your hand. The app has no
  over-the-air update and no longer needs to know your Wi-Fi password.
- When a collar is set up from the browser, the base refuses its own radio, and stops if a
  different device answers after the collar restarts, so the lock never lands on the wrong one.

## Data and access

Collars, fences and recent positions are kept in the app's `/data` folder (`geocollar.json`) and
included in Home Assistant backups. The app keeps an hourly copy beside it, `geocollar.json.bak`.
If a power cut damages the data file, the app starts from that copy, keeps the damaged file under
a `.damaged-` name, and says so in its log. Through the sidebar the page is behind your Home Assistant login. The
optional port on the **Network** tab opens the page without any login, so leave it empty unless
you need it and trust your network.
