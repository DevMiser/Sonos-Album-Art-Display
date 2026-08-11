# The following program is provided by DevMiser - https://github.com/DevMiser

#!/usr/bin/env python3

import io
import json
import logging
import os
import socket
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import soco
from PIL import Image

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "hide")
import pygame

# --- Configuration ---

SONOS_ZONE = "Family Room"     # First-run default zone (web UI choice overrides)
POLL_INTERVAL = 2.0            # Seconds between Sonos polls
IDLE_GRACE_SECONDS = 5.0        # Keep showing after pause/stop before screen-off
WAKE_PREVIEW_SECONDS = 5.0     # How long a touch shows the screen while idle
TOUCH_DEBOUNCE_SECONDS = 0.5
OVERLAY_SECONDS = 5.0          # How long the control overlay stays up after a touch
VOLUME_SEND_INTERVAL = 0.2     # Min seconds between volume commands while dragging

WEB_PORT = 8080                # Zone-switcher / setup page port
WIFI_IFACE = "wlan0"
HOTSPOT_SSID = "AlbumArtDisplay-Setup"
HOTSPOT_CON = "AlbumArtDisplaySetup"   # NetworkManager connection profile name
HOTSPOT_IP = "10.42.0.1"               # NetworkManager's default for shared mode
NO_WIFI_GRACE_SECONDS = 30     # How long Wi-Fi must be down before hotspot starts
                               # (only used when there's no saved Wi-Fi profile at all)
WIFI_CHECK_INTERVAL = 30.0

UNDETECTED_GRACE_SECONDS = 90   # Known SSID never seen in a scan for this long -> setup
STUCK_BACKSTOP_SECONDS = 3600   # Known SSID stays visible but never connects -> setup anyway
HOTSPOT_RETRY_INTERVAL = 300    # While hotspot is up, retest the known network this often
HOTSPOT_RETRY_TEST_SECONDS = 20 # How long to give NetworkManager to reconnect during a retest
NUDGE_DELAY_SECONDS = 600       # Post-startup: nudge the radio after this long fully offline

REBOOT_ESCALATION_ENABLED = True     # set False to disable this feature entirely
REBOOT_ESCALATION_SECONDS = 1800     # 30 min continuously offline -> escalate to reboot
MAX_AUTO_REBOOTS_PER_DAY = 3         # safety cap to avoid a reboot loop
REBOOT_HISTORY_PATH = os.path.expanduser("~/.sonos_album_art_reboots.json")
AUTOSTART_DESKTOP_PATH = os.path.expanduser("~/.config/autostart/sonos-album-art.desktop")

CRASH_LOOP_MAX = 5              # max crashes allowed within the window
CRASH_LOOP_WINDOW_SECONDS = 600 # 10 minutes -- stop retrying if exceeded

BACKGROUND = (0, 0, 0)
TEXT_COLOR = (173, 216, 230)
OVERLAY_COLOR = (255, 255, 255)

# Font sizes and pixel gaps below are designed against a 800-px-tall
# screen and scaled by (actual height / DESIGN_HEIGHT) at startup.
DESIGN_HEIGHT = 800.0

# Physical width/height ratio of a square drawn in equal pixels, keyed by
# resolution. The Hosyond 7" 800x480 DSI panel has non-square pixels (a
# pixel square measures 91mm x 85mm with calipers), so album art must be
# drawn narrower to appear square. Unlisted resolutions get 1.0 (square pixels)
PIXEL_ASPECTS = {(800, 480): 91 / 85}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SETUP_PAGE = "album_art_setup.html"
ZONES_PAGE = "album_art_zones.html"
CONFIG_PATH = os.path.expanduser("~/.sonos_album_art.json")

# --- Logging ---
# Console only, no log file — run this manually in a terminal to see
# live diagnostics; nothing is written to disk or visible under autostart.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("sonos_album_art")

# --- Persistent settings ---

def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}

def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError as e:
        log.warning("Could not save config: %s", e)

# --------------------------------------------------------------------------
# Screen power control
# --------------------------------------------------------------------------

class ScreenPower:

    def __init__(self):
        # 'sysfs' | 'ddc-power' | 'ddc-brightness' | 'xset' | 'wlr'
        # | 'vcgencmd' | 'black' | None (legacy probe pending)
        self.method = None
        self.wlr_output = None
        self.is_off = False
        self.bl_path = None
        self.bl_restore = None       # sysfs brightness to restore on wake
        self.ddc_restore = 100       # ddc brightness to restore on wake
        self._ddc_lock = threading.Lock()
        self._probe_backlight()

    def _run_ok(self, cmd, timeout=4):
        try:
            return subprocess.run(cmd, capture_output=True, timeout=timeout).returncode == 0
        except Exception:
            return False

    def _ddc_read(self, cmd):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                return r.stdout.strip()
        except Exception:
            pass
        return None

    def _probe_backlight(self):
        # 1. sysfs backlight — instant, true backlight off. Start at maximum
        # brightness on launch; a manual adjustment via the panel's own
        # button afterward is picked up dynamically (see
        # _refresh_backlight_restore_value) and remembered from then on.
        try:
            entries = sorted(os.listdir("/sys/class/backlight"))
        except OSError:
            entries = []
        for entry in entries:
            path = f"/sys/class/backlight/{entry}/brightness"
            max_path = f"/sys/class/backlight/{entry}/max_brightness"
            try:
                with open(max_path) as f:
                    max_val = int(f.read().strip())
                with open(path, "w") as f:
                    f.write(str(max_val))
                self.bl_path = path
                self.bl_restore = max_val
                self.method = "sysfs"
                log.info("Screen-off method: sysfs (%s), starting at max brightness (%d)",
                         path, max_val)
                return
            except (OSError, ValueError):
                continue

        # 2. DDC/CI over the HDMI cable (the 8DP-CAPLCD supports it).
        # VCP D6 = power mode (1 on, 4 standby) — kills the backlight while
        # the video signal keeps running, so wake is instant.
        if self._ddc_read(["ddcutil", "getvcp", "d6", "--brief"]) is not None:
            self.method = "ddc-power"
            log.info("Screen-off method: ddc-power (VCP D6)")
            return
        out = self._ddc_read(["ddcutil", "getvcp", "10", "--brief"])
        if out is not None:
            try:
                # --brief output: "VCP 10 C <current> <max>"
                self.ddc_restore = max(1, int(out.split()[3]))
            except Exception:
                self.ddc_restore = 100
            self.method = "ddc-brightness"
            log.info("Screen-off method: ddc-brightness (restore=%d)", self.ddc_restore)
            return

        # 3. Legacy signal-off methods are probed at first off() — probing
        # them here would blank the screen at startup.
        log.info("No backlight control found; will probe signal-off methods.")

    def off(self):
        if self.is_off:
            return
        self.is_off = True
        if self.method == "sysfs":
            self._refresh_backlight_restore_value()
            self._write_backlight(0)
        elif self.method in ("ddc-power", "ddc-brightness"):
            self._ddc_async(on=False)
        elif self.method is None:
            self._probe_legacy_off()
        else:
            self._apply(on=False)

    def on(self):
        if not self.is_off:
            return
        self.is_off = False
        if self.method == "sysfs":
            self._write_backlight(self.bl_restore)
        elif self.method in ("ddc-power", "ddc-brightness"):
            self._ddc_async(on=True)
        else:
            self._apply(on=True)

    def shutdown(self):
        """Synchronously restore the panel on exit — never leave it dark."""
        self.is_off = False
        if self.method == "sysfs" and self.bl_path:
            self._write_backlight(self.bl_restore)
        elif self.method == "ddc-power":
            self._run_ok(["ddcutil", "setvcp", "d6", "1"], timeout=15)
        elif self.method == "ddc-brightness":
            self._run_ok(["ddcutil", "setvcp", "10", str(self.ddc_restore)], timeout=15)
        else:
            self._apply(on=True)

    def _refresh_backlight_restore_value(self):
        """Re-reads the current sysfs brightness right before dimming, so a
        manual adjustment (e.g. via a physical button on the panel) since
        the last wake is remembered and restored next time, instead of
        reverting to the stale value captured once at startup."""
        try:
            with open(self.bl_path) as f:
                cur = int(f.read().strip())
            if cur > 0:
                self.bl_restore = cur
        except (OSError, ValueError):
            pass

    def _write_backlight(self, value):
        try:
            with open(self.bl_path, "w") as f:
                f.write(str(value))
        except OSError as e:
            log.warning("Backlight write failed: %s", e)

    def _ddc_async(self, on):
        """ddcutil takes ~1-2 s, so transitions run off the render loop."""
        if self.method == "ddc-power":
            cmd = ["ddcutil", "setvcp", "d6", "1" if on else "4"]
        else:
            cmd = ["ddcutil", "setvcp", "10",
                   str(self.ddc_restore) if on else "0"]

        def _do():
            with self._ddc_lock:
                if not self._run_ok(cmd, timeout=15):
                    log.warning("ddcutil failed: %s", " ".join(cmd))
        threading.Thread(target=_do, daemon=True).start()

    def _detect_wlr_output(self):
        try:
            r = subprocess.run(["wlr-randr"], capture_output=True, text=True, timeout=4)
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    if line and not line[0].isspace():
                        return line.split()[0]
        except Exception:
            pass
        return None

    def _probe_legacy_off(self):
        if self._run_ok(["xset", "dpms", "force", "off"]):
            self.method = "xset"
        else:
            self.wlr_output = self._detect_wlr_output()
            if self.wlr_output and self._run_ok(
                    ["wlr-randr", "--output", self.wlr_output, "--off"]):
                self.method = "wlr"
            elif self._run_ok(["vcgencmd", "display_power", "0"]):
                self.method = "vcgencmd"
            else:
                self.method = "black"
        log.info("Screen-off method: %s", self.method)

    def _apply(self, on):
        if self.method == "xset":
            self._run_ok(["xset", "dpms", "force", "on" if on else "off"])
        elif self.method == "wlr":
            self._run_ok(["wlr-randr", "--output", self.wlr_output,
                          "--on" if on else "--off"])
        elif self.method == "vcgencmd":
            self._run_ok(["vcgencmd", "display_power", "1" if on else "0"])
        # 'black' is handled by the render loop drawing a black frame

# --------------------------------------------------------------------------
# Wi-Fi management: connectivity watch + setup hotspot (NetworkManager)
# --------------------------------------------------------------------------

class WifiManager(threading.Thread):
    """Runs a one-time Wi-Fi connectivity check at startup only. If the Pi
    isn't online (including a known SSID that's visible but won't actually
    connect), it brings up an open hotspot so a phone can join and provision
    credentials through the setup web page.

    Once online is reached for the first time, this switches to a
    lightweight background watchdog for the rest of the run. After 
    REBOOT_ESCALATION_SECONDS (optional, see
    REBOOT_ESCALATION_ENABLED), it escalates to a full reboot."""

    def __init__(self):
        super().__init__(daemon=True)
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.state = "starting"   # starting | online | hotspot | connecting
        self.status_msg = ""      # extra line shown on the setup screen
        self.scan_cache = []      # last good scan (AP mode usually can't scan)
        self._down_since = None
        self._last_seen_known = None   # last time a saved SSID was scan-visible

    def _nmcli(self, *args, timeout=20):
        try:
            r = subprocess.run(["nmcli", *args], capture_output=True,
                               text=True, timeout=timeout)
            return r.returncode == 0, (r.stdout or "") + (r.stderr or "")
        except Exception as e:
            return False, str(e)

    def wifi_online(self):
        """True when a real Wi-Fi connection (not our hotspot) is active."""
        ok, out = self._nmcli("-t", "-f", "NAME,TYPE,DEVICE",
                              "connection", "show", "--active", timeout=10)
        if not ok:
            return False
        for line in out.splitlines():
            parts = line.split(":")
            if (len(parts) >= 3 and parts[1] in ("802-11-wireless", "wifi")
                    and parts[0] != HOTSPOT_CON):
                return True
        return False

    def scan_networks(self):
        """Returns [{ssid, signal, open}] sorted by signal, or None on
        failure (typical while the hotspot has the radio in AP mode)."""
        ok, out = self._nmcli("-t", "-f", "SSID,SIGNAL,SECURITY",
                              "device", "wifi", "list", "--rescan", "yes",
                              timeout=25)
        if not ok:
            return None
        best = {}
        for line in out.splitlines():
            parts = line.rsplit(":", 2)
            if len(parts) != 3:
                continue
            ssid = parts[0].replace("\\:", ":").strip()
            if not ssid:
                continue          # hidden network
            try:
                signal = int(parts[1])
            except ValueError:
                signal = 0
            is_open = parts[2].strip() in ("", "--")
            if ssid not in best or signal > best[ssid]["signal"]:
                best[ssid] = {"ssid": ssid, "signal": signal, "open": is_open}
        return sorted(best.values(), key=lambda n: -n["signal"])

    def _known_ssids(self):
        """SSIDs of saved (not necessarily active) Wi-Fi profiles, excluding
        our own setup hotspot."""
        ok, out = self._nmcli("-t", "-f", "NAME,TYPE", "connection", "show")
        if not ok:
            return set()
        ssids = set()
        for line in out.splitlines():
            parts = line.rsplit(":", 1)
            if len(parts) != 2:
                continue
            name, conn_type = parts[0].replace("\\:", ":").strip(), parts[1].strip()
            if conn_type != "802-11-wireless" or name == HOTSPOT_CON:
                continue
            ok2, ssid_out = self._nmcli("-g", "802-11-wireless.ssid",
                                        "connection", "show", name, timeout=10)
            ssid = ssid_out.strip() if ok2 else ""
            ssids.add(ssid or name)
        return ssids

    def _known_network_visible(self, known_ssids):
        """True if any saved SSID currently shows up in a scan."""
        nets = self.scan_networks()
        if nets is None:
            return False
        if nets:
            self.scan_cache = nets
        visible = {n["ssid"] for n in nets}
        return bool(known_ssids & visible)

    def run(self):
        next_hotspot_retry = None
        while not self.stop_event.is_set():
            try:
                with self.lock:
                    state = self.state

                if state != "hotspot":
                    next_hotspot_retry = None

                if state == "connecting":
                    # A submit_wifi worker owns the radio right now
                    self.stop_event.wait(2)
                    continue

                if state == "hotspot":
                    # Periodically step out of AP mode to test whether the
                    # known network has come back, so a temporary outage that
                    # outlasted the grace period doesn't require re-provisioning.
                    now = time.time()
                    if next_hotspot_retry is None:
                        next_hotspot_retry = now + HOTSPOT_RETRY_INTERVAL
                    elif now >= next_hotspot_retry:
                        if self._retry_known_network():
                            next_hotspot_retry = None
                            self._down_since = None
                            self._last_seen_known = None
                            continue
                        next_hotspot_retry = now + HOTSPOT_RETRY_INTERVAL
                    self.stop_event.wait(WIFI_CHECK_INTERVAL)
                    continue

                if self.wifi_online():
                    if state != "online":
                        self._teardown_hotspot()
                        with self.lock:
                            self.state = "online"
                            self.status_msg = ""
                    self._down_since = None
                    self._last_seen_known = None
                    log.info("Wi-Fi confirmed online — startup check complete; "
                             "switching to a lightweight background watchdog.")
                    break
                else:
                    # Note: state can never be "online" here — phase 1 exits
                    # (see break above) as soon as it's first reached, so
                    # this branch only ever runs during the initial startup
                    # check, before online has been reached even once.
                    now = time.time()
                    if self._down_since is None:
                        self._down_since = now

                    known_ssids = self._known_ssids()
                    if not known_ssids:
                        # Never configured — nothing to detect, so fall back to
                        # the original short grace period.
                        if now - self._down_since >= NO_WIFI_GRACE_SECONDS:
                            self._start_hotspot()
                    else:
                        if self._known_network_visible(known_ssids):
                            self._last_seen_known = now
                        last_seen = self._last_seen_known or self._down_since
                        undetected_for = now - last_seen
                        stuck_for = now - self._down_since
                        if (undetected_for >= UNDETECTED_GRACE_SECONDS
                                or stuck_for >= STUCK_BACKSTOP_SECONDS):
                            self._start_hotspot()
                self.stop_event.wait(WIFI_CHECK_INTERVAL)
            except Exception as e:
                log.warning("WifiManager error, retrying: %s", e)
                self.stop_event.wait(WIFI_CHECK_INTERVAL)

        # --- Phase 2: lightweight watchdog for the rest of the run ---
        down_since = None
        next_nudge = None
        rebooted_this_outage = False
        while not self.stop_event.is_set():
            try:
                if self.wifi_online():
                    down_since = None
                    next_nudge = None
                    rebooted_this_outage = False
                else:
                    now = time.time()
                    if down_since is None:
                        down_since = now
                        log.warning(
                            "Wi-Fi appears down; will nudge the radio after "
                            "%d minutes if it doesn't reconnect on its own.",
                            NUDGE_DELAY_SECONDS // 60)
                    if next_nudge is None:
                        next_nudge = down_since + NUDGE_DELAY_SECONDS
                    if now >= next_nudge:
                        self._nudge_wifi()
                        next_nudge = now + NUDGE_DELAY_SECONDS
                    if (REBOOT_ESCALATION_ENABLED and not rebooted_this_outage
                            and now - down_since >= REBOOT_ESCALATION_SECONDS):
                        rebooted_this_outage = True
                        self._escalate_to_reboot()
            except Exception as e:
                log.warning("Wi-Fi watchdog error: %s", e)
            self.stop_event.wait(WIFI_CHECK_INTERVAL)

    def _nudge_wifi(self):
        """Power-cycles the Wi-Fi radio to force NetworkManager to restart
        association from scratch."""
        log.warning("Nudging Wi-Fi radio after prolonged disconnection.")
        self._nmcli("radio", "wifi", "off", timeout=10)
        time.sleep(3)
        self._nmcli("radio", "wifi", "on", timeout=10)

    def _reboot_allowed(self):
        """True if fewer than MAX_AUTO_REBOOTS_PER_DAY auto-reboots have
        happened in the last 24 hours; records this attempt if so."""
        now = time.time()
        try:
            with open(REBOOT_HISTORY_PATH, encoding="utf-8") as f:
                history = json.load(f)
        except (OSError, ValueError):
            history = []
        history = [t for t in history if now - t < 86400]
        if len(history) >= MAX_AUTO_REBOOTS_PER_DAY:
            return False
        history.append(now)
        try:
            with open(REBOOT_HISTORY_PATH, "w", encoding="utf-8") as f:
                json.dump(history, f)
        except OSError:
            pass
        return True

    def _escalate_to_reboot(self):
        """Last resort after the lightweight nudge hasn't restored
        connectivity for a long time: reboot the Pi, which fully resets the
        Wi-Fi hardware's own reset line."""
        if not os.path.exists(AUTOSTART_DESKTOP_PATH):
            log.warning(
                "Wi-Fi still down after %d minutes, but autostart isn't "
                "configured — skipping automatic reboot (it wouldn't bring "
                "the display back on its own). See SONOSREADME.md.",
                REBOOT_ESCALATION_SECONDS // 60)
            return
        if not self._reboot_allowed():
            log.warning(
                "Wi-Fi still down after %d minutes, but the daily "
                "auto-reboot limit (%d) has been reached — continuing to "
                "nudge only.", REBOOT_ESCALATION_SECONDS // 60,
                MAX_AUTO_REBOOTS_PER_DAY)
            return
        log.warning(
            "Wi-Fi still down after %d minutes — rebooting to fully reset "
            "the Wi-Fi hardware.", REBOOT_ESCALATION_SECONDS // 60)
        subprocess.run(["sudo", "reboot"])

    def _start_hotspot(self):
        # Scan while the radio can still scan; the cache feeds /api/scan
        # once AP mode blocks scanning.
        nets = self.scan_networks()
        if nets:
            self.scan_cache = nets
        self._nmcli("connection", "delete", HOTSPOT_CON)
        ok, out = self._nmcli(
            "connection", "add", "type", "wifi", "ifname", WIFI_IFACE,
            "con-name", HOTSPOT_CON, "autoconnect", "no",
            "ssid", HOTSPOT_SSID,
            "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
            "ipv4.method", "shared", "ipv6.method", "disabled")
        if ok:
            ok, out = self._nmcli("connection", "up", HOTSPOT_CON, timeout=30)
        if ok:
            with self.lock:
                self.state = "hotspot"
                self.status_msg = ""
            log.info("Setup hotspot '%s' up — page at http://%s:%d/",
                     HOTSPOT_SSID, HOTSPOT_IP, WEB_PORT)
        else:
            log.warning("Hotspot failed: %s", out.strip())
            self._down_since = time.time()   # retry after another grace period

    def _teardown_hotspot(self):
        self._nmcli("connection", "down", HOTSPOT_CON, timeout=15)
        self._nmcli("connection", "delete", HOTSPOT_CON, timeout=15)

    def _retry_known_network(self):
        """Briefly drops the setup hotspot so NetworkManager can attempt any
        saved Wi-Fi profile; re-raises the hotspot if it doesn't reconnect."""
        log.info("Hotspot retry: testing for the known network...")
        self._nmcli("connection", "down", HOTSPOT_CON, timeout=15)
        deadline = time.time() + HOTSPOT_RETRY_TEST_SECONDS
        while time.time() < deadline:
            if self.wifi_online():
                self._nmcli("connection", "delete", HOTSPOT_CON)
                with self.lock:
                    self.state = "online"
                    self.status_msg = ""
                log.info("Known network reconnected — hotspot retired.")
                return True
            time.sleep(2)
        ok, out = self._nmcli("connection", "up", HOTSPOT_CON, timeout=30)
        if not ok:
            log.warning("Re-raising hotspot failed: %s", out.strip())
        return False

    def submit_wifi(self, ssid, password):
        """Called from the web server when the setup page posts credentials.
        Runs in a worker so the HTTP response goes out before the hotspot
        (and the phone's connection to it) is eliminated."""
        def _do():
            with self.lock:
                self.state = "connecting"
                self.status_msg = f"Connecting to “{ssid}”…"
            time.sleep(2)                       # let the phone get its 200 OK
            self._nmcli("connection", "down", HOTSPOT_CON, timeout=15)
            self._nmcli("connection", "delete", ssid)   # drop stale profile
            args = ["device", "wifi", "connect", ssid]
            if password:
                args += ["password", password]
            ok, out = self._nmcli(*args, timeout=60)
            if ok and self.wifi_online():
                self._teardown_hotspot()
                with self.lock:
                    self.state = "online"
                    self.status_msg = ""
                log.info("Joined Wi-Fi '%s'.", ssid)
            else:
                log.warning("Joining '%s' failed: %s", ssid, out.strip())
                self._nmcli("connection", "delete", ssid)
                ok2, _ = self._nmcli("connection", "up", HOTSPOT_CON, timeout=30)
                with self.lock:
                    self.state = "hotspot" if ok2 else "starting"
                    self.status_msg = (f"Couldn't join “{ssid}” — "
                                       "check the password and try again.")
                self._down_since = time.time() if ok2 else 0
        threading.Thread(target=_do, daemon=True).start()

    def snapshot(self):
        with self.lock:
            return self.state, self.status_msg

    def shutdown(self):
        self.stop_event.set()
        state, _ = self.snapshot()
        if state in ("hotspot", "connecting"):
            self._teardown_hotspot()

# --------------------------------------------------------------------------
# Sonos polling thread
# --------------------------------------------------------------------------

class SonosWatcher(threading.Thread):
    """Polls the selected zone and keeps the latest track data + album art
    (already scaled to a pygame surface) available for the render loop.
    The zone can be switched via set_zone()."""

    def __init__(self, art_w, art_h, zone_name):
        super().__init__(daemon=True)
        self.art_w = art_w      # may differ from art_h on non-square-pixel panels
        self.art_h = art_h
        self.lock = threading.Lock()
        self.stop_event = threading.Event()

        self.zone_name = zone_name
        self.coordinator = None
        self.playing = False
        self.transport_state = "STOPPED"
        self.title = ""
        self.artist = ""
        self.album = ""
        self.art_surface = None
        self.version = 0            # bumped whenever displayed data changes
        self.last_playing_time = 0.0

        self._speaker = None
        self._last_known_ip = None  # last speaker IP, tried first before SSDP
        self._track_key = None
        self._art_retry = False
        self._fail_count = 0
        self._disc_warned = None    # last discovery warning, to avoid repeats

    def get_zone(self):
        with self.lock:
            return self.zone_name

    def set_zone(self, name):
        """Switches the displayed zone (called from the web server)."""
        with self.lock:
            if name == self.zone_name:
                return
            self.zone_name = name
            self.playing = False
            self.title = self.artist = self.album = ""
            self.art_surface = None
            self.version += 1
        self._speaker = None
        self._track_key = None
        self._art_retry = False

    def _warn_once(self, msg):
        """Discovery runs every 10 s while offline — only log a warning
        when it changes, so the log doesn't flood in hotspot mode."""
        if msg != self._disc_warned:
            log.warning(msg)
            self._disc_warned = msg

    def _discover(self):
        zone = self.get_zone()

        if self._last_known_ip:
            try:
                candidate = soco.SoCo(self._last_known_ip)
                if candidate.player_name.lower() == zone.lower():
                    self._speaker = candidate
                    self._disc_warned = None
                    return True
            except Exception:
                pass   # fall through to full SSDP discovery

        try:
            speakers = soco.discover(timeout=5)
        except Exception as e:
            self._warn_once(f"Discovery failed: {e}")
            return False
        if not speakers:
            self._warn_once("No Sonos speakers found.")
            return False
        for spk in speakers:
            try:
                if spk.player_name.lower() == zone.lower():
                    self._speaker = spk
                    self._last_known_ip = spk.ip_address
                    self._disc_warned = None
                    return True
            except Exception:
                continue   # a malformed/unreachable speaker shouldn't abort the search
        try:
            names = ", ".join(sorted(s.player_name for s in speakers))
        except Exception:
            names = "(unavailable)"
        self._warn_once(f"Zone '{zone}' not found. Available zones: {names}")
        return False

    def run(self):
        while not self.stop_event.is_set():
            try:
                if self._speaker is None:
                    if not self._discover():
                        self.stop_event.wait(10)
                        continue
                self._poll()
                self._fail_count = 0
            except Exception as e:
                self._fail_count += 1
                if self._fail_count >= 5:
                    log.warning("Repeated poll failures, rediscovering: %s", e)
                    self._speaker = None
                    self._fail_count = 0
            self.stop_event.wait(POLL_INTERVAL)

    def _poll(self):
        speaker = self._speaker
        if speaker is None:       # zone switched mid-cycle
            return
        coordinator = speaker.group.coordinator
        self.coordinator = coordinator

        transport = coordinator.get_current_transport_info()
        state = transport.get("current_transport_state", "STOPPED")
        info = coordinator.get_current_track_info()
        uri = (info.get("uri") or "").strip()

        # TV / line-in sources have no album art — treat as not playing
        av_source = uri.startswith("x-sonos-htastream") or uri.startswith("x-rincon-stream")
        playing = state in ("PLAYING", "TRANSITIONING") and not av_source

        with self.lock:
            self.transport_state = state
            self.playing = playing
            if playing:
                self.last_playing_time = time.time()

        if not playing:
            self._track_key = None
            self._art_retry = False
            return

        title = (info.get("title") or "").strip() or "Music"
        artist = (info.get("artist") or "").strip()
        album = (info.get("album") or "").strip()
        art_url = (info.get("album_art") or "").strip()

        key = f"{title}|{artist}|{album}|{art_url}"
        if key != self._track_key:
            self._track_key = key
            art = self._fetch_art(art_url)
            self._set_display(title, artist, album, art)
            self._art_retry = art is None and bool(art_url)
        elif self._art_retry:
            art = self._fetch_art(art_url)
            if art is not None:
                self._set_display(title, artist, album, art)
                self._art_retry = False

    def _set_display(self, title, artist, album, art_surface):
        with self.lock:
            self.title, self.artist, self.album = title, artist, album
            self.art_surface = art_surface
            self.version += 1

    def _fetch_art(self, art_url):
        if not art_url:
            return None
        url = art_url
        if url.startswith("/"):
            ip = (self.coordinator or self._speaker).ip_address
            url = f"http://{ip}:1400{url}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = resp.read()
            img = Image.open(io.BytesIO(data))
            img = img.resize((self.art_w, self.art_h), Image.LANCZOS)
            if img.mode == "RGBA":
                bg = Image.new("RGB", img.size, BACKGROUND)
                bg.paste(img, mask=img.split()[3])
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")
            return pygame.image.frombuffer(img.tobytes(), img.size, "RGB")
        except Exception:
            return None

# --------------------------------------------------------------------------
# Zone directory for the web UI
# --------------------------------------------------------------------------

class ZoneDirectory:

    def __init__(self):
        self.lock = threading.Lock()
        self._speakers = {}
        self._scanned = 0.0

    def speakers(self, max_age=25):
        with self.lock:
            if time.time() - self._scanned < max_age and self._speakers:
                return dict(self._speakers)
        try:
            found = soco.discover(timeout=4) or set()
        except Exception:
            found = set()
        with self.lock:
            if found:
                self._speakers = {s.player_name: s for s in found}
                self._scanned = time.time()
            return dict(self._speakers)

    def zone_list(self, current_zone):
        zones = []
        for name, spk in sorted(self.speakers().items()):
            now = ""
            try:
                coord = spk.group.coordinator
                st = coord.get_current_transport_info().get(
                    "current_transport_state", "")
                if st == "PLAYING":
                    info = coord.get_current_track_info()
                    title = (info.get("title") or "").strip()
                    artist = (info.get("artist") or "").strip()
                    now = " — ".join(p for p in (title, artist) if p)
            except Exception:
                pass
            zones.append({
                "name": name,
                "current": name.lower() == current_zone.lower(),
                "now": now,
            })
        return zones

# --------------------------------------------------------------------------
# Web server: setup portal + zone switcher
# --------------------------------------------------------------------------

class WebHandler(BaseHTTPRequestHandler):
    # Wired up by start_web_server()
    watcher = None
    wifi = None
    zones = None
    server_version = "SonosAlbumArt/1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_page(self, filename):
        path = os.path.join(SCRIPT_DIR, filename)
        try:
            with open(path, encoding="utf-8") as f:
                html = f.read()
        except OSError:
            self._send(500, json.dumps({"error": f"{filename} missing on Pi"}))
            return
        html = html.replace("__HOSTNAME__", socket.gethostname())
        self._send(200, html, "text/html")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            state, _ = self.wifi.snapshot()
            self._send_page(SETUP_PAGE if state in ("hotspot", "connecting")
                            else ZONES_PAGE)
        elif path == "/setup":
            self._send_page(SETUP_PAGE)
        elif path == "/zones":
            self._send_page(ZONES_PAGE)
        elif path == "/api/scan":
            nets = self.wifi.scan_networks()
            if nets:
                self.wifi.scan_cache = nets
            else:
                nets = self.wifi.scan_cache
            self._send(200, json.dumps({"networks": nets}))
        elif path == "/api/zones":
            current = self.watcher.get_zone()
            self._send(200, json.dumps({"zones": self.zones.zone_list(current)}))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            self._send(400, json.dumps({"error": "bad request"}))
            return
        if self.path == "/api/wifi":
            ssid = (payload.get("ssid") or "").strip()
            if not ssid:
                self._send(400, json.dumps({"error": "ssid required"}))
                return
            self.wifi.submit_wifi(ssid, payload.get("password") or "")
            self._send(200, json.dumps({"ok": True}))
        elif self.path == "/api/zone":
            name = (payload.get("name") or "").strip()
            by_lower = {n.lower(): n for n in self.zones.speakers()}
            real = by_lower.get(name.lower())
            if real is None:
                self._send(404, json.dumps({"error": "unknown zone"}))
                return
            self.watcher.set_zone(real)
            cfg = load_config()
            cfg["zone"] = real
            save_config(cfg)
            self._send(200, json.dumps({"ok": True}))
        else:
            self._send(404, json.dumps({"error": "not found"}))

def start_web_server(watcher, wifi, zones):
    WebHandler.watcher = watcher
    WebHandler.wifi = wifi
    WebHandler.zones = zones
    try:
        server = ThreadingHTTPServer(("0.0.0.0", WEB_PORT), WebHandler)
    except OSError as e:
        log.warning("Web server failed to start on port %d: %s", WEB_PORT, e)
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Zone switcher: http://%s.local:%d/", socket.gethostname(), WEB_PORT)
    return server

# --------------------------------------------------------------------------
# Touch transport commands
# --------------------------------------------------------------------------

def send_transport(watcher, action):
    """Runs a transport command in a worker thread so touch never blocks
    the render loop."""
    def _do():
        try:
            spk = watcher.coordinator
            if spk is None:
                return
            if action == "toggle":
                st = spk.get_current_transport_info().get("current_transport_state", "")
                if st == "PLAYING":
                    spk.pause()
                else:
                    spk.play()
            elif action == "next":
                spk.next()
            elif action == "previous":
                spk.previous()
        except Exception as e:
            log.warning("Transport '%s' failed: %s", action, e)
    threading.Thread(target=_do, daemon=True).start()

# --------------------------------------------------------------------------
# Pop-up control overlay
# --------------------------------------------------------------------------

def draw_glyph(screen, name, rect, color):
    """Draws a transport glyph centered in rect."""
    s = int(rect.height * 0.5)
    x = rect.centerx - s // 2
    y = rect.centery - s // 2
    bar = max(3, int(s * 0.08))   # end-bar width for next/previous
    if name == "pause":
        bw = int(s * 0.3)
        pygame.draw.rect(screen, color, (x, y, bw, s))
        pygame.draw.rect(screen, color, (x + s - bw, y, bw, s))
    elif name == "play":
        pygame.draw.polygon(screen, color, [(x, y), (x + s, y + s // 2), (x, y + s)])
    elif name == "stop":
        pygame.draw.rect(screen, color, (x, y, s, s))
    elif name == "next":
        half = s // 2
        pygame.draw.polygon(screen, color, [(x, y), (x + half, y + s // 2), (x, y + s)])
        pygame.draw.polygon(screen, color, [(x + half, y), (x + s, y + s // 2), (x + half, y + s)])
        pygame.draw.rect(screen, color, (x + s - bar, y, bar, s))
    elif name == "previous":
        half = s // 2
        pygame.draw.polygon(screen, color, [(x + half, y), (x, y + s // 2), (x + half, y + s)])
        pygame.draw.polygon(screen, color, [(x + s, y), (x + half, y + s // 2), (x + s, y + s)])
        pygame.draw.rect(screen, color, (x, y, bar, s))


class ControlOverlay:

    def __init__(self, watcher, art_w, art_h, screen_h):
        self.watcher = watcher
        self.lock = threading.Lock()
        self.visible_until = 0.0
        self.volume = None           # 0-100, fetched when the overlay opens
        self.dragging = False
        self.pressed = None          # (action, highlight-until)
        self._last_send = 0.0

        panel_w = int(art_w * 0.80)
        panel_h = int(art_h * 0.36)
        px = (art_w - panel_w) // 2
        py = (screen_h - panel_h) // 2
        self.panel = pygame.Rect(px, py, panel_w, panel_h)
        self.panel_radius = max(8, int(art_h * 0.03))
        self.hl_radius = max(6, int(art_h * 0.02))

        btn = int(panel_h * 0.52)
        gap = (panel_w - 3 * btn) // 4
        by = py + int(panel_h * 0.10)
        self.buttons = []
        for i, action in enumerate(("previous", "toggle", "next")):
            bx = px + gap + i * (btn + gap)
            self.buttons.append((action, pygame.Rect(bx, by, btn, btn)))

        margin = int(panel_w * 0.08)
        track_h = max(4, int(art_h * 0.008))
        ty = by + btn + int(panel_h * 0.22)
        self.track = pygame.Rect(px + margin, ty - track_h // 2,
                                 panel_w - 2 * margin, track_h)
        self.knob_r = max(10, int(art_h * 0.02))
        self.slider_band = self.track.inflate(
            self.knob_r * 2 + 20, max(48, int(art_h * 0.10)))

    def visible(self, now):
        return now < self.visible_until

    def show(self, now):
        self.visible_until = now + OVERLAY_SECONDS
        self._fetch_volume_async()

    def handle_down(self, pos, now):
        """Hit-tests a touch while visible. Returns a transport action name
        for the main loop to dispatch, or None (slider / empty space)."""
        self.visible_until = now + OVERLAY_SECONDS
        if self.slider_band.collidepoint(pos):
            self.dragging = True
            self._set_from_x(pos[0], now, force=True)
            return None
        for action, rect in self.buttons:
            if rect.collidepoint(pos):
                self.pressed = (action, now + 0.4)
                return action
        return None

    def handle_motion(self, pos, now):
        if self.dragging:
            self.visible_until = now + OVERLAY_SECONDS
            self._set_from_x(pos[0], now)

    def handle_up(self, now):
        if self.dragging:
            self.dragging = False
            with self.lock:
                v = self.volume
            if v is not None:
                self._send_volume_async(v)

    def _set_from_x(self, x, now, force=False):
        frac = (x - self.track.left) / self.track.width
        v = max(0, min(100, int(round(frac * 100))))
        with self.lock:
            self.volume = v
        if force or now - self._last_send >= VOLUME_SEND_INTERVAL:
            self._last_send = now
            self._send_volume_async(v)

    def _group(self):
        spk = self.watcher.coordinator
        return spk.group if spk is not None else None

    def _fetch_volume_async(self):
        if self.dragging:
            return
        def _do():
            try:
                grp = self._group()
                if grp is None:
                    return
                v = grp.volume
                with self.lock:
                    if not self.dragging:
                        self.volume = v
            except Exception as e:
                log.warning("Volume read failed: %s", e)
        threading.Thread(target=_do, daemon=True).start()

    def _send_volume_async(self, v):
        def _do():
            try:
                grp = self._group()
                if grp is not None:
                    grp.volume = int(v)
            except Exception as e:
                log.warning("Volume set failed: %s", e)
        threading.Thread(target=_do, daemon=True).start()

    def draw(self, screen, now):
        if not self.visible(now):
            return

        backing = pygame.Surface(self.panel.size, pygame.SRCALPHA)
        pygame.draw.rect(backing, (0, 0, 0, 150), backing.get_rect(),
                         border_radius=self.panel_radius)
        screen.blit(backing, self.panel.topleft)

        with self.watcher.lock:
            playing = self.watcher.transport_state == "PLAYING"
        with self.lock:
            vol = self.volume

        for action, rect in self.buttons:
            if self.pressed and self.pressed[0] == action and now < self.pressed[1]:
                hl = pygame.Surface(rect.size, pygame.SRCALPHA)
                pygame.draw.rect(hl, (255, 255, 255, 60), hl.get_rect(),
                                 border_radius=self.hl_radius)
                screen.blit(hl, rect.topleft)
            if action == "toggle":
                glyph = "pause" if playing else "play"
            else:
                glyph = action
            draw_glyph(screen, glyph, rect, OVERLAY_COLOR)

        track_radius = max(2, self.track.height // 2)
        pygame.draw.rect(screen, (140, 140, 140), self.track,
                         border_radius=track_radius)
        if vol is not None:
            fill_w = int(self.track.width * vol / 100)
            if fill_w > 0:
                pygame.draw.rect(screen, OVERLAY_COLOR,
                                 (self.track.left, self.track.top, fill_w, self.track.height),
                                 border_radius=track_radius)
            pygame.draw.circle(screen, OVERLAY_COLOR,
                               (self.track.left + fill_w, self.track.centery), self.knob_r)

# --------------------------------------------------------------------------
# Text wrapping
# --------------------------------------------------------------------------

def _break_long_word(word, font, max_width_px):
    BREAK_CHARS = "/_-."
    pieces, buf = [], ""
    for ch in word:
        buf += ch
        if ch in BREAK_CHARS:
            pieces.append(buf)
            buf = ""
    if buf:
        pieces.append(buf)
    chunks, cur = [], ""
    for piece in pieces:
        if font.size(piece)[0] > max_width_px:
            if cur:
                chunks.append(cur)
                cur = ""
            for ch in piece:
                if font.size(cur + ch)[0] <= max_width_px:
                    cur += ch
                else:
                    if cur:
                        chunks.append(cur)
                    cur = ch
            continue
        if font.size(cur + piece)[0] <= max_width_px:
            cur += piece
        else:
            if cur:
                chunks.append(cur)
            cur = piece
    if cur:
        chunks.append(cur)
    return chunks

def wrap_text(text, font, max_width_px):
    words = text.split()
    if not words:
        return []
    lines, current = [], ""

    def flush():
        nonlocal current
        if current:
            lines.append(current)
            current = ""

    for word in words:
        if font.size(word)[0] <= max_width_px:
            candidate = (current + " " + word) if current else word
            if font.size(candidate)[0] <= max_width_px:
                current = candidate
            else:
                flush()
                current = word
            continue
        flush()
        for piece in _break_long_word(word, font, max_width_px):
            if not current:
                current = piece
            elif font.size(current + piece)[0] <= max_width_px:
                current = current + piece
            else:
                flush()
                current = piece
    flush()
    return lines

# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

_DEJAVU_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_DEJAVU_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_font_fallback_warned = False

def _load_font(size, bold=False):
    global _font_fallback_warned
    path = _DEJAVU_BOLD if bold else _DEJAVU_REGULAR
    if os.path.isfile(path):
        try:
            return pygame.font.Font(path, size)
        except Exception:
            pass
    if not _font_fallback_warned:
        log.warning("DejaVu Sans not found at %s — falling back to "
                    "SysFont (font size may vary at early boot).", path)
        _font_fallback_warned = True
    return pygame.font.SysFont("DejaVu Sans", size, bold=bold)


_title_font_cache = {}   # (title, column_w, base_size) -> pygame Font

def fit_title_font(title, column_w, base_size):
    """Largest bold title font <= base_size whose longest single word fits
    the text column, so titles shrink slightly instead of breaking a word
    across two lines. Floored at 70% of base_size; cached per track."""
    key = (title, column_w, base_size)
    font = _title_font_cache.get(key)
    if font is not None:
        return font
    floor_size = max(12, int(base_size * 0.7))
    size = base_size
    while True:
        font = _load_font(size, bold=True)
        widest = max((font.size(w)[0] for w in title.split()), default=0)
        if widest <= column_w or size <= floor_size:
            break
        size = max(floor_size, size - 2)
    if len(_title_font_cache) > 64:
        _title_font_cache.clear()
    _title_font_cache[key] = font
    return font

def draw_now_playing(screen, snap, fonts, width, height, art_w, text_scale):
    screen.fill(BACKGROUND)

    if snap["art"] is not None:
        screen.blit(snap["art"], (0, (height - snap["art"].get_height()) // 2))
    else:
        ph = fonts["placeholder"].render("♪", True, TEXT_COLOR)
        screen.blit(ph, ph.get_rect(center=(art_w // 2, height // 2)))

    text_left = art_w + int(width * 0.025)
    column_w = max(50, width - text_left - int(width * 0.025))

    sections = []
    if snap["title"]:
        title_font = fit_title_font(snap["title"], column_w, fonts["title_size"])
        sections.append((snap["title"], title_font, int(30 * text_scale)))
    if snap["album"]:
        sections.append((snap["album"], fonts["album"], int(20 * text_scale)))
    if snap["artist"]:
        sections.append((snap["artist"], fonts["artist"], 0))

    rendered, total = [], 0
    for text, font, gap in sections:
        for line in wrap_text(text, font, column_w) or [text]:
            surf = font.render(line, True, TEXT_COLOR)
            rendered.append((surf, font.get_linesize()))
            total += font.get_linesize()
        rendered.append((None, gap))
        total += gap

    y = max(0, (height - total) // 2)
    for surf, advance in rendered:
        if surf is not None:
            screen.blit(surf, (text_left, y))
        y += advance

def draw_idle_message(screen, fonts, width, height):
    screen.fill(BACKGROUND)
    msg = fonts["idle"].render("Nothing playing", True, TEXT_COLOR)
    screen.blit(msg, msg.get_rect(center=(width // 2, height // 2)))

def draw_setup_screen(screen, fonts, width, height, wifi, text_scale):
    screen.fill(BACKGROUND)
    state, status = wifi.snapshot()

    lines = [
        ("Wi-Fi Setup", fonts["title"], int(40 * text_scale)),
        ("This album art display is not connected to Wi-Fi.",
         fonts["artist"], int(30 * text_scale)),
        (f"1.  On your iPhone, join the Wi-Fi network “{HOTSPOT_SSID}”",
         fonts["artist"], int(14 * text_scale)),
        (f"2.  In Safari, open  http://{HOTSPOT_IP}:{WEB_PORT}",
         fonts["artist"], int(14 * text_scale)),
        ("3.  Pick your home network and enter its password",
         fonts["artist"], int(14 * text_scale)),
    ]
    if state == "connecting":
        lines.append((status or "Connecting…", fonts["album"], 0))
    elif status:
        lines.append((status, fonts["album"], 0))

    margin = int(width * 0.06)
    column_w = width - 2 * margin
    rendered, total = [], 0
    for text, font, gap in lines:
        for line in wrap_text(text, font, column_w) or [text]:
            surf = font.render(line, True, TEXT_COLOR)
            rendered.append((surf, font.get_linesize()))
            total += font.get_linesize()
        rendered.append((None, gap))
        total += gap

    y = max(0, (height - total) // 2)
    for surf, advance in rendered:
        if surf is not None:
            screen.blit(surf, (margin, y))
        y += advance

# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def event_pos(event, width, height):
    if event.type in (pygame.FINGERDOWN, pygame.FINGERMOTION, pygame.FINGERUP):
        return (event.x * width, event.y * height)
    return event.pos


def _wait_for_stable_display_info(min_checks=3, check_interval=0.5, max_wait=15.0):
    started = time.time()
    last = None
    stable_count = 0
    deadline = started + max_wait
    info = pygame.display.Info()
    while time.time() < deadline:
        info = pygame.display.Info()
        size = (info.current_w, info.current_h)
        if size == last:
            stable_count += 1
            if stable_count >= min_checks:
                break
        else:
            stable_count = 0
        last = size
        time.sleep(check_interval)
    waited = time.time() - started
    if waited > check_interval:
        log.info("Display resolution settled at %dx%d after %.1fs",
                 info.current_w, info.current_h, waited)
    return info

def run_display():
    pygame.init()
    info = _wait_for_stable_display_info()
    screen = pygame.display.set_mode(
        (info.current_w, info.current_h), pygame.FULLSCREEN)
    width, height = screen.get_size()
    pygame.display.set_caption("Sonos Album Art")
    pygame.mouse.set_visible(False)

    pixel_aspect = PIXEL_ASPECTS.get((width, height), 1.0)
    art_h = min(height, int(width * 0.65))
    art_w = int(art_h / pixel_aspect)
    scale = height / DESIGN_HEIGHT
    text_scale = scale ** 0.6

    title_size = max(16, int(50 * text_scale))
    fonts = {
        "title": _load_font(title_size, bold=True),
        "title_size": title_size,
        "album": _load_font(max(13, int(40 * text_scale)), bold=True),
        "artist": _load_font(max(12, int(34 * text_scale))),
        "placeholder": _load_font(int(art_h * 0.25)),
        "idle": _load_font(max(14, int(44 * text_scale))),
    }

    zone = load_config().get("zone", SONOS_ZONE)

    watcher = SonosWatcher(art_w, art_h, zone)
    watcher.start()
    wifi = WifiManager()
    wifi.start()
    zones = ZoneDirectory()
    start_web_server(watcher, wifi, zones)
    power = ScreenPower()
    overlay = ControlOverlay(watcher, art_w, art_h, height)
    clock = pygame.time.Clock()

    preview_until = 0.0
    last_touch = 0.0

    try:
        running = True
        while running:
            now = time.time()
            with watcher.lock:
                snap = {
                    "playing": watcher.playing,
                    "last_playing": watcher.last_playing_time,
                    "title": watcher.title,
                    "album": watcher.album,
                    "artist": watcher.artist,
                    "art": watcher.art_surface,
                    "have_track": bool(watcher.title),
                }
            wifi_state, _ = wifi.snapshot()

            if wifi_state in ("hotspot", "connecting"):
                mode = "setup"
            elif snap["playing"] or (
                    snap["have_track"]
                    and now - snap["last_playing"] < IDLE_GRACE_SECONDS):
                mode = "showing"
            elif now < preview_until:
                mode = "preview"
            else:
                mode = "dark"

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN and event.key in (
                        pygame.K_ESCAPE, pygame.K_q):
                    running = False
                elif event.type in (pygame.MOUSEBUTTONDOWN, pygame.FINGERDOWN):
                    if now - last_touch < TOUCH_DEBOUNCE_SECONDS:
                        continue
                    last_touch = now
                    if mode == "setup":
                        continue      # setup screen has no touch controls
                    pos = event_pos(event, width, height)

                    if mode == "dark":
                        # Wake the screen and show the controls right away
                        preview_until = now + WAKE_PREVIEW_SECONDS
                        overlay.show(now)
                    else:
                        if mode == "preview":
                            preview_until = now + WAKE_PREVIEW_SECONDS
                        if not overlay.visible(now):
                            overlay.show(now)
                        else:
                            action = overlay.handle_down(pos, now)
                            if action:
                                send_transport(watcher, action)
                elif event.type in (pygame.MOUSEMOTION, pygame.FINGERMOTION):
                    if mode == "setup":
                        continue
                    if overlay.dragging and mode == "preview":
                        preview_until = now + WAKE_PREVIEW_SECONDS
                    overlay.handle_motion(event_pos(event, width, height), now)
                elif event.type in (pygame.MOUSEBUTTONUP, pygame.FINGERUP):
                    overlay.handle_up(now)

            if mode == "dark":
                power.off()
                screen.fill(BACKGROUND)
            elif mode == "setup":
                power.on()
                draw_setup_screen(screen, fonts, width, height, wifi, text_scale)
            else:
                power.on()
                if mode == "showing" or snap["have_track"]:
                    draw_now_playing(screen, snap, fonts, width, height,
                                     art_w, text_scale)
                else:
                    draw_idle_message(screen, fonts, width, height)
                overlay.draw(screen, now)

            pygame.display.flip()
            # Higher frame rate while the overlay is up keeps slider drags smooth
            clock.tick(30 if overlay.visible(now) else 10)
    finally:
        watcher.stop_event.set()
        wifi.shutdown()    # tear down the hotspot if it was active
        power.shutdown()   # never leave the panel off on exit
        pygame.quit()

def main():
    log.info("Sonos Album Art starting")
    crash_times = []
    while True:
        try:
            run_display()
            return
        except Exception:
            log.exception("Display crashed — restarting in 5 seconds")
            try:
                pygame.quit()
            except Exception:
                pass
            now = time.time()
            crash_times = [t for t in crash_times if now - t < CRASH_LOOP_WINDOW_SECONDS]
            crash_times.append(now)
            if len(crash_times) >= CRASH_LOOP_MAX:
                log.error(
                    "Crashed %d times in %d minutes — giving up instead of "
                    "continuing to restart.", len(crash_times),
                    CRASH_LOOP_WINDOW_SECONDS // 60)
                return
            time.sleep(5)

if __name__ == "__main__":
    main()
