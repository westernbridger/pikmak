"""Mac side: receives input from the PC and replays it as native macOS events.

Run:  python3 mac_client.py            (needs Accessibility permission, see README)
      python3 mac_client.py --dry-run  (any OS: prints events instead of injecting them)
"""

import argparse
import os
import platform
import socket
import subprocess
import sys
import threading
import time

import common


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ----------------------------------------------------------------------------- injectors

MOD_FLAGS_KEYS = {
    common.MAC_SHIFT: "shift", common.MAC_RSHIFT: "shift",
    common.MAC_CTRL: "ctrl", common.MAC_RCTRL: "ctrl",
    common.MAC_OPT: "alt", common.MAC_ROPT: "alt",
    common.MAC_CMD: "cmd", common.MAC_RCMD: "cmd",
}


class BaseInjector:
    """Screen geometry, cursor bookkeeping and edge detection shared by real and dry-run modes."""

    def __init__(self, cfg):
        self.side = cfg["mac_side"].lower()   # where the Mac sits relative to the PC
        self.speed = float(cfg.get("mouse_speed", 1.0))
        self.invert = bool(cfg.get("invert_scroll", False))
        self.lines_per_notch = float(cfg.get("scroll_lines_per_notch", 3))
        self.buttons = set()
        self.keys = set()
        self.mods = set()
        self.caps = False
        self.wheel_acc = [0.0, 0.0]
        self.last_click = (None, 0.0, 0.0, 0.0, 0)  # button, time, x, y, count

    # subclasses provide: bounds(), cursor(), _post_move(x, y), _post_button(b, down, x, y, n),
    #                     _post_key(code, down, repeat, is_mod), _post_scroll(dx, dy), media(k)

    def enter(self, pos):
        x0, y0, x1, y1 = self.bounds()
        pos = min(max(pos, 0.0), 1.0)
        if self.side == "right":      # Mac is right of the PC -> come in on the Mac's left edge
            x, y = x0 + 2, y0 + pos * (y1 - y0)
        elif self.side == "left":
            x, y = x1 - 3, y0 + pos * (y1 - y0)
        elif self.side == "bottom":
            x, y = x0 + pos * (x1 - x0), y0 + 2
        else:
            x, y = x0 + pos * (x1 - x0), y1 - 3
        self._post_move(x, y)

    def move(self, dx, dy):
        """Move by a delta. Returns the edge fraction if the cursor should go back to the PC."""
        x0, y0, x1, y1 = self.bounds()
        cx, cy = self.cursor()
        x, y = cx + dx * self.speed, cy + dy * self.speed
        if not self.buttons:  # never hand back mid-drag
            if self.side == "right" and x < x0:
                return (y - y0) / max(1, y1 - y0)
            if self.side == "left" and x > x1 - 1:
                return (y - y0) / max(1, y1 - y0)
            if self.side == "bottom" and y < y0:
                return (x - x0) / max(1, x1 - x0)
            if self.side == "top" and y > y1 - 1:
                return (x - x0) / max(1, x1 - x0)
        x = min(max(x, x0), x1 - 1)
        y = min(max(y, y0), y1 - 1)
        self._post_move(x, y, dx, dy)
        return None

    def button(self, b, down):
        x, y = self.cursor()
        if down:
            lb, lt, lx, ly, n = self.last_click
            now = time.monotonic()
            if lb == b and now - lt < 0.4 and abs(x - lx) < 5 and abs(y - ly) < 5:
                n += 1
            else:
                n = 1
            self.last_click = (b, now, x, y, n)
            self.buttons.add(b)
        else:
            self.buttons.discard(b)
            n = self.last_click[4] if self.last_click[0] == b else 1
        self._post_button(b, down, x, y, n)

    def wheel(self, dx, dy):
        sign = -1 if self.invert else 1
        self.wheel_acc[0] += sign * dx / 120.0 * self.lines_per_notch
        self.wheel_acc[1] += sign * dy / 120.0 * self.lines_per_notch
        lx, ly = int(self.wheel_acc[0]), int(self.wheel_acc[1])
        self.wheel_acc[0] -= lx
        self.wheel_acc[1] -= ly
        if lx or ly:
            self._post_scroll(lx, ly)

    def key(self, code, down):
        is_mod = code in MOD_FLAGS_KEYS or code == common.MAC_CAPS
        repeat = down and code in self.keys
        if down:
            self.keys.add(code)
            if code in MOD_FLAGS_KEYS:
                self.mods.add(code)
            elif code == common.MAC_CAPS and not repeat:
                self.caps = not self.caps
        else:
            self.keys.discard(code)
            self.mods.discard(code)
        self._post_key(code, down, repeat, is_mod)

    def release_all(self):
        for code in list(self.keys):
            self.key(code, False)
        for b in list(self.buttons):
            self.button(b, False)


class DryRunInjector(BaseInjector):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.pos = [720.0, 450.0]

    def bounds(self):
        return (0, 0, 1440, 900)

    def cursor(self):
        return tuple(self.pos)

    def _post_move(self, x, y, dx=0, dy=0):
        self.pos = [x, y]
        log(f"move -> ({x:.0f}, {y:.0f})")

    def _post_button(self, b, down, x, y, n):
        log(f"button {b} {'down' if down else 'up'} clicks={n}")

    def _post_key(self, code, down, repeat, is_mod):
        log(f"key 0x{code:02X} {'down' if down else 'up'}{' (repeat)' if repeat else ''}")

    def _post_scroll(self, dx, dy):
        log(f"scroll dx={dx} dy={dy}")

    def media(self, k):
        log(f"media {k}")


class QuartzInjector(BaseInjector):
    MEDIA = {"volume_up": 0, "volume_down": 1, "mute": 7, "play": 16, "next": 17, "previous": 18}

    def __init__(self, cfg):
        super().__init__(cfg)
        import Quartz as Q
        self.Q = Q
        self._bounds = None
        self._bounds_t = 0.0
        self.BTN = {
            "left": (Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp, Q.kCGEventLeftMouseDragged, 0),
            "right": (Q.kCGEventRightMouseDown, Q.kCGEventRightMouseUp, Q.kCGEventRightMouseDragged, 1),
            "middle": (Q.kCGEventOtherMouseDown, Q.kCGEventOtherMouseUp, Q.kCGEventOtherMouseDragged, 2),
            "x1": (Q.kCGEventOtherMouseDown, Q.kCGEventOtherMouseUp, Q.kCGEventOtherMouseDragged, 3),
            "x2": (Q.kCGEventOtherMouseDown, Q.kCGEventOtherMouseUp, Q.kCGEventOtherMouseDragged, 4),
        }
        self.FLAG = {"shift": Q.kCGEventFlagMaskShift, "ctrl": Q.kCGEventFlagMaskControl,
                     "alt": Q.kCGEventFlagMaskAlternate, "cmd": Q.kCGEventFlagMaskCommand}

    def bounds(self):
        # union of all active displays, refreshed every few seconds
        now = time.monotonic()
        if self._bounds is None or now - self._bounds_t > 3:
            Q = self.Q
            err, ids, n = Q.CGGetActiveDisplayList(16, None, None)
            rects = [Q.CGDisplayBounds(d) for d in ids[:n]] or [Q.CGDisplayBounds(Q.CGMainDisplayID())]
            self._bounds = (
                min(r.origin.x for r in rects), min(r.origin.y for r in rects),
                max(r.origin.x + r.size.width for r in rects),
                max(r.origin.y + r.size.height for r in rects),
            )
            self._bounds_t = now
        return self._bounds

    def cursor(self):
        p = self.Q.CGEventGetLocation(self.Q.CGEventCreate(None))
        return (p.x, p.y)

    def flags(self):
        f = 0
        for code in self.mods:
            f |= self.FLAG[MOD_FLAGS_KEYS[code]]
        if self.caps:
            f |= self.Q.kCGEventFlagMaskAlphaShift
        return f

    def _post(self, ev):
        self.Q.CGEventPost(self.Q.kCGHIDEventTap, ev)

    def _post_move(self, x, y, dx=0, dy=0):
        Q = self.Q
        held = next((b for b in ("left", "right", "middle", "x1", "x2") if b in self.buttons), None)
        if held:
            _, _, etype, num = self.BTN[held]
        else:
            etype, num = Q.kCGEventMouseMoved, 0
        ev = Q.CGEventCreateMouseEvent(None, etype, (x, y), min(num, 2))
        if num > 2:
            Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventButtonNumber, num)
        Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventDeltaX, int(dx))
        Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventDeltaY, int(dy))
        Q.CGEventSetFlags(ev, self.flags())
        self._post(ev)

    def _post_button(self, b, down, x, y, n):
        Q = self.Q
        tdown, tup, _, num = self.BTN[b]
        ev = Q.CGEventCreateMouseEvent(None, tdown if down else tup, (x, y), min(num, 2))
        Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventButtonNumber, num)
        Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventClickState, n)
        Q.CGEventSetFlags(ev, self.flags())
        self._post(ev)

    def _post_key(self, code, down, repeat, is_mod):
        Q = self.Q
        ev = Q.CGEventCreateKeyboardEvent(None, code, down)
        if is_mod:
            Q.CGEventSetType(ev, Q.kCGEventFlagsChanged)
        elif repeat:
            Q.CGEventSetIntegerValueField(ev, Q.kCGKeyboardEventAutorepeat, 1)
        Q.CGEventSetFlags(ev, self.flags())
        self._post(ev)

    def _post_scroll(self, dx, dy):
        Q = self.Q
        ev = Q.CGEventCreateScrollWheelEvent(None, Q.kCGScrollEventUnitLine, 2, dy, dx)
        Q.CGEventSetFlags(ev, self.flags())
        self._post(ev)

    def media(self, k):
        key = self.MEDIA.get(k)
        if key is None:
            return
        from AppKit import NSEvent
        for state in (0xA, 0xB):  # key down, key up
            ev = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
                14, (0, 0), state << 8, 0, 0, None, 8, (key << 16) | (state << 8), -1)
            self._post(ev.CGEvent())


def check_accessibility():
    try:
        import Quartz as Q
        if hasattr(Q, "CGPreflightPostEventAccess"):
            if Q.CGPreflightPostEventAccess():
                return True
            Q.CGRequestPostEventAccess()
            return False
    except Exception:
        pass
    return True  # can't tell on this macOS/pyobjc version; carry on


# ----------------------------------------------------------------------------- clipboard

def mac_clipboard_get():
    try:
        out = subprocess.run(["pbpaste"], capture_output=True, timeout=2,
                             env={**os.environ, "LANG": "en_US.UTF-8"})
        return out.stdout.decode("utf-8", errors="replace")
    except Exception:
        return None


def mac_clipboard_set(text):
    try:
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), timeout=2,
                       env={**os.environ, "LANG": "en_US.UTF-8"})
    except Exception:
        pass


# ----------------------------------------------------------------------------- client

class Client:
    def __init__(self, cfg, injector, clipboard):
        self.cfg = cfg
        self.inj = injector
        self.clipboard = clipboard and cfg.get("clipboard_sync", True)
        self.active = False
        self.last_clip = None

    def discover(self):
        port = self.cfg["port"] + 1
        tag = common.discovery_tag(self.cfg["passphrase"])
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", port))
        s.settimeout(1.0)
        log(f"Looking for the PC on the LAN (UDP {port})...")
        try:
            while True:
                try:
                    data, addr = s.recvfrom(256)
                except socket.timeout:
                    continue
                parts = data.split(b"|")
                if len(parts) == 3 and parts[0] == common.DISCOVERY_MAGIC and parts[1] == tag:
                    return addr[0], int(parts[2])
        finally:
            s.close()

    def run_forever(self):
        while True:
            host, port = self.cfg.get("pc_host"), self.cfg["port"]
            if not host:
                host, port = self.discover()
            try:
                log(f"Connecting to {host}:{port} ...")
                sock = socket.create_connection((host, port), timeout=5)
                self.session(sock)
            except (OSError, ConnectionError, ValueError) as e:
                log(f"Connection lost/failed: {e}")
            self.inj.release_all()
            self.active = False
            time.sleep(2)

    def session(self, sock):
        sock.settimeout(8)
        chan = common.SecureChannel.handshake(sock, self.cfg["passphrase"], is_server=False)
        x0, y0, x1, y1 = self.inj.bounds()
        chan.send({"t": "hello", "name": platform.node(), "w": int(x1 - x0), "h": int(y1 - y0)})
        log("Connected. Push the mouse off the PC screen's "
            f"{self.cfg['mac_side']} edge to come over here.")
        stop = threading.Event()

        def pinger():
            while not stop.wait(2.0):
                try:
                    chan.send({"t": "ping"})
                except OSError:
                    return

        threading.Thread(target=pinger, daemon=True).start()
        try:
            while True:
                msg = chan.recv()
                self.handle(chan, msg)
        finally:
            stop.set()
            chan.close()

    def handle(self, chan, msg):
        t = msg.get("t")
        if t == "ping":
            return
        if t == "enter":
            self.active = True
            self.inj.enter(float(msg.get("pos", 0.5)))
            log("<- control arrived from PC")
        elif t == "clip":
            text = msg.get("text") or ""
            self.last_clip = text
            if self.clipboard:
                mac_clipboard_set(text)
        elif not self.active:
            return
        elif t == "move":
            frac = self.inj.move(float(msg["dx"]), float(msg["dy"]))
            if frac is not None:
                self.hand_back(chan, frac)
        elif t == "btn":
            self.inj.button(msg["b"], bool(msg["down"]))
        elif t == "wheel":
            self.inj.wheel(float(msg.get("dx", 0)), float(msg.get("dy", 0)))
        elif t == "key":
            self.inj.key(int(msg["code"]), bool(msg["down"]))
        elif t == "media":
            self.inj.media(msg.get("k"))
        elif t == "leave":
            self.inj.release_all()
            self.active = False
            self._send_clip(chan)
            log("-> control returned to PC (hotkey)")

    def _send_clip(self, chan):
        if not self.clipboard:
            return
        text = mac_clipboard_get()
        if text and text != self.last_clip and len(text) < 1_000_000:
            self.last_clip = text
            chan.send({"t": "clip", "text": text})

    def hand_back(self, chan, frac):
        self.inj.release_all()
        self.active = False
        self._send_clip(chan)
        chan.send({"t": "leave", "pos": frac})
        log("-> control returned to PC")


def main():
    ap = argparse.ArgumentParser(description="PC-Mac KVM client (runs on the Mac)")
    ap.add_argument("--dry-run", action="store_true", help="print events instead of injecting")
    ap.add_argument("--host", help="PC address (overrides config.json pc_host)")
    args = ap.parse_args()

    cfg = common.load_config()
    if args.host:
        cfg["pc_host"] = args.host

    if args.dry_run:
        inj, clip = DryRunInjector(cfg), False
    else:
        if sys.platform != "darwin":
            raise SystemExit("mac_client.py injects input on macOS only (use --dry-run elsewhere).")
        if not check_accessibility():
            log("This app needs Accessibility permission to control the Mac.")
            log("System Settings > Privacy & Security > Accessibility: enable your Terminal "
                "(or Python), then run this again.")
            sys.exit(1)
        inj, clip = QuartzInjector(cfg), True

    log("PC-Mac KVM client" + (" [dry run]" if args.dry_run else ""))
    try:
        Client(cfg, inj, clip).run_forever()
    except KeyboardInterrupt:
        inj.release_all()
        log("Bye")


if __name__ == "__main__":
    main()
