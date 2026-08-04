# Sonos Album Art Display
### Album Art Display for Raspberry Pi 3 Model A+

Sonos Album Art Display shows the album art from the currently playing track on your Sonos music system. It runs on a Raspberry Pi 3 Model A+ (or more current version) with an attached 7-inch DSI touchscreen. The album art fills the left square of the screen, with the song, album, and artist shown on the right. The display's backlight turns fully off when nothing is playing and wakes automatically when music starts.

Touch the screen at any time to bring up on-screen transport controls — previous track, pause/resume, next track, and a sliding volume control — which fade away a few seconds after your last touch. If the Pi cannot automatically connect to a Wi-Fi network at startup, it broadcasts its own temporary Wi-Fi hotspot so you can connect it to the Wi-Fi network from your phone. A small built-in web page also lets you switch which Sonos zone is shown from any device on your network.

No AI, no wake word, no API keys — everything is read directly from your Sonos speaker over your local network via the SoCo library.

---

## How to Run Sonos Album Art Display on a Raspberry Pi 3 A+

The following steps are required:

- Obtain the necessary hardware — listed below
- Follow the steps below to prepare your Raspberry Pi 3 A+ and install the software
- 3D print the frame (optional)

---

## Hardware Requirements

**Raspberry Pi 3 Model A+** — A [Raspberry Pi 3 Model A+](https://www.adafruit.com/product/4027) is recommended because it is affordable and more than powerful enough for this program.

**5V Power Supply** — Use a [5V Micro-USB Power Supply](https://www.amazon.com/dp/B07CVH21NC/). The Pi 3 A+ uses a micro-USB power connector, not the USB-C connector found on later Raspberry Pi models.

**Hosyond 7-inch DSI Touchscreen** — The [Hosyond 7-inch Touchscreen IPS DSI Display](https://www.amazon.com/Hosyond-Touchscreen-Compatible-Capacitive-Driver-Free/dp/B0D3QB7X4Z) is the one this project and its 3D-printable frame are designed and tested for. It connects to the Raspberry Pi entirely through the DSI ribbon cable — no separate USB cable is needed for touch, which is convenient since the Pi 3 A+ has only one USB port.

> **Note:** This software is resolution-independent — fonts and on-screen layout automatically scale to whatever screen resolution is detected at startup, so a different display should generally work without any code changes. If you use a different screen, follow that display's own manufacturer instructions for connecting it to the Raspberry Pi rather than the Hosyond-specific steps below. The one Hosyond-specific detail in the code is `PIXEL_ASPECTS`, a small correction for this particular panel's non-square pixels (see **Configuration Reference**) — if your alternate display also has non-square pixels and album art looks stretched, you would add a similar entry for its resolution.

**MicroSD Card** — A 16 GB or larger card rated **C10** and **A1** from a reputable brand is recommended. Purchase from a reputable retailer to avoid counterfeit cards. I used a [Ultra Plus](https://www.amazon.com/Adapter-Memory-Tablet-Console-TF162/dp/B0CYT2DVSQ/).

**Phillips Flat Head Screws (Optional)** — You will need four [Phillips flat head screws](https://boltdepot.com/Product-Details?product=6854) if you decide to use the optional 3D printed frame. The ones used elsewhere in this project are 2.5 x 0.45 x 8mm.

---

## Prepare Your Raspberry Pi 3 A+

These instructions assume you already have a Raspberry Pi 3 A+ set up and running **Raspberry Pi OS (64-bit, Bookworm or later)**. If not, use the Raspberry Pi Imager to install it, which is available here: https://www.raspberrypi.com/software/. Be sure to use the **64-bit** version.

> **Note:** The Debian-based OS often asks for the Pi's password when asked to take certain actions, including many commands that begin with "sudo". Whenever asked, type the password for your device and press Enter.

### 1. Update Your System

Open a terminal and enter the following commands in order:

```
sudo apt update
sudo apt full-upgrade
```

If asked whether you want to continue, enter **Y** and press Enter. When the upgrade completes, reboot:

```
sudo reboot
```

Log back in after the reboot.

### 2. Connect the Hosyond Display

Carefully open the DSI connector latch on your Raspberry Pi board and insert the display's DSI ribbon cable, making sure the gold contacts are oriented correctly at both ends. The Hosyond panel is marketed as driver-free/plug-and-play, so no manual configuration is required.

### 3. Reorient the Display Screen (Optional)

If you are going to put the display in the 3D-printed or other frame and want to have the power port at a particular side of the frame, you may need to reorient the axes of the display screen. To do so, follow the display rotation instructions on the manufacturer's site: https://hosyond.com/.

Then reboot:

```
sudo reboot
```

### 4. Install System-Level Dependencies

Some packages must be installed at the system level via `apt`. Open a terminal and enter the following commands in order:

```
sudo apt install x11-xserver-utils
sudo apt-get install libsdl2-dev libsdl2-image-dev libsdl2-mixer-dev libsdl2-ttf-dev
sudo apt install python3-pygame python3-pil network-manager
```

If asked whether you want to continue, enter **Y** and press Enter.

> **Note:** Raspberry Pi OS images typically already include `python3-pygame`, `python3-pil`, and NetworkManager (`nmcli`) — the command above is a safety net in case any are missing. Unlike an HDMI display, this DSI touchscreen exposes its backlight directly through the Linux `sysfs` interface, so **`ddcutil` is not needed** for this project on this display — if you see a log line mentioning a different screen-off method being probed, that's just the fallback chain being checked, not an error.

### 5. Clone the Repository

Open a terminal and enter the following commands:

```
cd /home/pi
git clone https://github.com/yourusername/Sonos_Album_Art.git
cd Sonos_Album_Art
```

> Replace `https://github.com/yourusername/Sonos_Album_Art.git` with the actual repository URL. If your clone creates a differently named folder, rename it or adjust the paths in the next steps accordingly. These instructions assume the files live at `/home/pi/SonosAlbumArt/`.

Move or copy the files into place:

```
mkdir -p /home/pi/SonosAlbumArt
cp -r /home/pi/Sonos_Album_Art/* /home/pi/SonosAlbumArt/
cd /home/pi/SonosAlbumArt
```

Confirm all three application files are present: `Sonos_Album_Art.py`, `album_art_setup.html`, and `album_art_zones.html`.

### 6. Install the One Missing Python Package

`pygame` and `Pillow` are already provided by the OS packages installed in step 4. The only package this project needs that isn't already on the system is `soco`, the library used to talk to Sonos speakers. No virtual environment or requirements file is needed for a single package — install it directly:

```
sudo apt install python3-soco
```

If that package isn't available in your repo mirror, install it with pip instead:

```
sudo pip3 install --break-system-packages soco
```

> **Note:** `--break-system-packages` is required on Bookworm and later because Debian blocks plain `pip install` outside a virtual environment by default (PEP 668). This is safe here because `soco` has no dependencies that conflict with the system-installed `pygame`/`Pillow`.

---

## Run the Program

Make sure your Hosyond DSI display is connected. Then open a terminal, navigate to the project folder, and run the script:

```
cd /home/pi/SonosAlbumArt
python3 Sonos_Album_Art.py
```

Wait for album art to appear, or the idle screen if nothing is currently playing.

---

## Using Sonos Album Art Display

### Now Playing

Whatever is playing on the configured Sonos zone fills the left square of the screen with album art, with the song title, album, and artist shown on the right. When nothing is playing, the screen continues to show the album art for a short period, then the backlight turns fully off. Playing something again wakes the display automatically.

### Touch Controls

Touch the screen at any time — whether it's showing album art, the idle message, or is fully dark — to bring up the transport controls: **previous track**, **pause/resume**, **next track**, and a sliding **volume** control for the applicable Sonos zone. The controls fade away 5 seconds after your last touch. Touching a dark screen wakes it and shows the controls immediately, so you can start playback right away.

### Wi-Fi Setup

This check runs only once, right when the program starts. If the Pi isn't online at startup and the network it was previously using can no longer be detected at all (for example, after moving the Pi to a new location), it broadcasts its own open Wi-Fi network named **AlbumArtDisplay-Setup** and shows join instructions on the screen. If the previously-used network is still detectable but just hasn't connected yet (for example, a router that's mid-reboot right as the Pi boots up), the program waits quietly instead of showing setup.

Once the program reaches a normal online state, it stops checking Wi-Fi entirely for the rest of that run — if the connection drops later during ongoing operation, the display does not react to it and relies on the Raspberry Pi's own networking to reconnect automatically once the network is back, exactly as it would for any device on your network. If you need to move the Pi to a new Wi-Fi network, unplug it, relocate it, power it back on and run the program again — the one-time setup check runs again on that fresh start.

To provision a new network:

1. On your phone, join the **AlbumArtDisplay-Setup** Wi-Fi network.
2. Open `http://10.42.0.1:8080/` in Safari or your browser.
3. Choose your Wi-Fi network from the list (or type its name), enter the password, and tap **Connect**.

The display will join the new network and the temporary hotspot will disappear.

> **Note:** Your Raspberry Pi and your Sonos system must be connected to the same Wi-Fi network for the album art display to work.

### Switching Sonos Zones

From any device on the same Wi-Fi network, open `http://<your-pi-hostname>.local:8080/` in a browser (the exact address is written to the log file at startup). This page lists every Sonos zone on your network along with what each is currently playing. Tap a zone to switch the display to it — your choice is saved and will still be selected the next time the program starts.

---

## Running Sonos Album Art Display Automatically at Startup (Optional)

After everything is working, you may want the display to launch automatically when the Raspberry Pi boots upon being plugged in. The most reliable way to do this is with an XDG autostart file, which tells the desktop session to launch the program automatically once the display and network are up.

Open a terminal and enter:

```
mkdir -p ~/.config/autostart
nano ~/.config/autostart/sonos-album-art.desktop
```

Add the following content:

```
[Desktop Entry]
Type=Application
Name=Sonos Album Art Display
Exec=/bin/bash -c 'cd /home/pi/SonosAlbumArt && python3 /home/pi/SonosAlbumArt/Sonos_Album_Art.py'
X-GNOME-Autostart-enabled=true
```

Press **Ctrl + X**, then **Y**, then **Enter** to save. The display will now launch automatically each time the Raspberry Pi boots.

> **Note:** If you ever need to stop it from launching at startup, delete or rename the file: `rm ~/.config/autostart/sonos-album-art.desktop`

> **Note:** If the cursor is showing on the display after using autostart, gently tap a finger on the touchscreen and the cursor will disappear.

---

## Configuration Reference

The following constants near the top of `Sonos_Album_Art.py` can be adjusted to suit your setup:

| Constant | Default | Description |
|---|---|---|
| `SONOS_ZONE` | `"Family Room"` | First-run default zone (overridden by whatever you later pick in the web UI, which persists) |
| `POLL_INTERVAL` | `2.0` seconds | How often the display checks the Sonos zone for track/state changes |
| `IDLE_GRACE_SECONDS` | `5.0` seconds | How long the screen keeps showing after playback pauses/stops before going dark |
| `WAKE_PREVIEW_SECONDS` | `5.0` seconds | How long a touch keeps the screen visible while idle |
| `OVERLAY_SECONDS` | `5.0` seconds | How long the touch control overlay stays up after the last touch |
| `WEB_PORT` | `8080` | Port for both the Wi-Fi setup page and the zone-switcher page |
| `NO_WIFI_GRACE_SECONDS` | `30` seconds | Grace period before the setup hotspot appears when the Pi has never been configured with any Wi-Fi network |
| `UNDETECTED_GRACE_SECONDS` | `90` seconds | How long a previously-known network must be undetectable in a scan before the setup hotspot appears |
| `STUCK_BACKSTOP_SECONDS` | `3600` seconds | Safety-net timeout if a known network stays visible but never actually connects (e.g. a changed password) |
| `HOTSPOT_RETRY_INTERVAL` | `300` seconds | While the setup hotspot is up, how often it briefly steps aside to retest the known network |
| `PIXEL_ASPECTS` | `{(800, 480): 91/85}` | Per-resolution correction so album art renders square on displays with non-square pixels (already set for the Hosyond 800x480 panel) |

---

## Project Structure

```
SonosAlbumArt/
├── Sonos_Album_Art.py       # Main program
├── album_art_setup.html     # Wi-Fi provisioning page, served during setup
├── album_art_zones.html     # Zone-switcher page, served during normal operation
├── .sonos_album_art.json    # Saved zone choice (created automatically, in your home folder)
└── sonos_album_art.log      # Log file (created automatically, in your home folder)
```

---

## Printing and Assembling the Enclosure

> **Draft:** the STL files and exact assembly steps for this frame are still being finalized. The outline below follows the same overall approach as this project's earlier enclosure design, with the speakerphone cradle removed since this project has no microphone/speaker hardware to house — update the specifics once the final print files are ready.

3D print the frame and tabs. The STLs for these 3D parts are on the same repository as the software. The recommended setting for slicing the STLs is 0.20mm quality with 30% infill.

Mount the Raspberry Pi to the back of the display per Hosyond's instructions. Insert the display into the frame and secure it with the tab screws, tightened only enough to hold the tabs in place — overtightening will squeeze the display and cause damage or distortions.

---

## Troubleshooting

**The display shows nothing on startup.**
Confirm the DSI ribbon cable is fully seated at both ends with the gold contacts oriented correctly. 

**Touch controls don't appear.**
The controls only appear when you touch the screen — they are not shown continuously. Tap anywhere on the screen and they should pop up within a moment.

**The Wi-Fi setup hotspot doesn't appear after losing Wi-Fi.**
This is expected — the Wi-Fi check only runs once at startup. Once the program has reached a normal online state, it never checks Wi-Fi again for the rest of that run, so a later disconnect never brings up the hotspot; the Pi's own networking reconnects on its own once the network is back. If you need to reprovision, unplug the Pi and power it back on so the startup check runs again.

**The zone-switcher page shows no zones, or "Could not reach the display."**
Confirm your phone/computer is on the same Wi-Fi network as the Pi, and that the hostname in the URL matches the Pi's actual hostname. Confirm Sonos speakers are powered on and reachable from the Pi's network.

**Sonos Album Art Display does not launch at startup (if you have set it to do so).**
Confirm the autostart file exists: `ls ~/.config/autostart/sonos-album-art.desktop`. To check whether the script is currently running, enter `pgrep -a python3` in a terminal.

