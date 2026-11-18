"""PC side (Windows): owns the keyboard and mouse.

Installs low-level keyboard/mouse hooks. While the cursor is on the PC, input passes
through untouched. When the cursor hits the screen edge facing the Mac, local input is
swallowed and forwarded (encrypted) to the Mac until the Mac cursor comes back.

Run:  python pc_server.py
"""

import ctypes
import ctypes.wintypes as wt
import queue
import socket
import sys
import threading
import time

import common

# ----------------------------------------------------------------------------- Win32

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)

WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
WM_QUIT = 0x0012
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
WM_MOUSEMOVE = 0x200
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x201, 0x202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x204, 0x205
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x207, 0x208
WM_MOUSEWHEEL, WM_XBUTTONDOWN, WM_XBUTTONUP, WM_MOUSEHWHEEL = 0x20A, 0x20B, 0x20C, 0x20E
LLKHF_EXTENDED, LLKHF_INJECTED = 0x01, 0x10
LLMHF_INJECTED = 0x01
MONITOR_DEFAULTTONEAREST = 2
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wt.POINT), ("mouseData", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT),
                ("dwFlags", wt.DWORD)]


user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
user32.SetWindowsHookExW.restype = wt.HHOOK
user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [wt.HHOOK]
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
user32.MonitorFromPoint.restype = wt.HMONITOR
user32.GetMonitorInfoW.argtypes = [wt.HMONITOR, ctypes.POINTER(MONITORINFO)]
user32.OpenClipboard.argtypes = [wt.HWND]
user32.GetClipboardData.argtypes = [wt.UINT]
user32.GetClipboardData.restype = wt.HANDLE
user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
user32.SetClipboardData.restype = wt.HANDLE
kernel32.GetModuleHandleW.restype = wt.HMODULE
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wt.HGLOBAL
kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
kernel32.GlobalFree.argtypes = [wt.HGLOBAL]


def set_dpi_aware():
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def monitor_rect(x, y):
    hmon = user32.MonitorFromPoint(wt.POINT(x, y), MONITOR_DEFAULTTONEAREST)
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
    r = mi.rcMonitor
    return (r.left, r.top, r.right, r.bottom)


def get_clipboard_text():
    for _ in range(5):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.02)
    else:
        return None
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = kernel32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text):
    data = text.encode("utf-16-le") + b"\x00\x00"
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h:
        return
    p = kernel32.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    kernel32.GlobalUnlock(h)
    for _ in range(5):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.02)
    else:
        kernel32.GlobalFree(h)
        return
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            kernel32.GlobalFree(h)
    finally:
        user32.CloseClipboard()


# ----------------------------------------------------------------------------- state

def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class Server:
    def __init__(self, cfg):
        self.cfg = cfg
        self.side = cfg["mac_side"].lower()
        self.dead = int(cfg.get("corner_dead_zone_px", 40))
        self.keymap = common.build_keymap(cfg.get("swap_alt_cmd", False))
        self.hotkey = [self._hotkey_set(n) for n in cfg.get("hotkey", [])]

        self.chan = None              # active SecureChannel to the Mac
        self.remote = False           # True while input goes to the Mac
        self.edge_mon = None          # monitor rect we left from
        self.park = (0, 0)            # where the PC cursor is parked while remote
        self.ignore_moves_until = 0   # GetTickCount ms; drops stale moves right after switching
        self.local_buttons = 0
        self.local_down = set()       # keys held on the PC when we switched (let their key-ups through)
        self.pressed = set()          # physical key state, for the hotkey
        self.events = queue.Queue()
        self.last_clip = None         # last clipboard text synced in either direction
        self.hook_thread_id = None
        self._lock = threading.RLock()

    @staticmethod
    def _hotkey_set(name):
        name = name.upper()
        if name in common.VK_GROUPS:
            return common.VK_GROUPS[name]
        if name in common.VK:
            return {common.VK[name]}
        raise SystemExit(f"Unknown hotkey key name: {name}")

    # ------------------------------------------------------------- switching

    def edge_hit(self, x, y):
        """Return the fraction along the edge (0..1) if (x, y) is on the Mac-facing edge."""
        l, t, r, b = monitor_rect(x, y)
        if self.side in ("left", "right"):
            if self.side == "right" and x < r - 1:
                return None
            if self.side == "left" and x > l:
                return None
            # must be the outermost monitor on that side
            probe = r if self.side == "right" else l - 1
            if monitor_rect(probe, y) != (l, t, r, b):
                return None
            if y < t + self.dead or y >= b - self.dead:
                return None
            frac = (y - t) / max(1, b - t)
        else:
            if self.side == "bottom" and y < b - 1:
                return None
            if self.side == "top" and y > t:
                return None
            probe = b if self.side == "bottom" else t - 1
            if monitor_rect(x, probe) != (l, t, r, b):
                return None
            if x < l + self.dead or x >= r - self.dead:
                return None
            frac = (x - l) / max(1, r - l)
        return frac, (l, t, r, b)

    def go_remote(self, frac, mon):
        with self._lock:
            if self.remote or self.chan is None:
                return False
            self.remote = True
            self.edge_mon = mon
            l, t, r, b = mon
            self.park = ((l + r) // 2, (t + b) // 2)
            self.local_down = set(self.pressed)
            self.ignore_moves_until = kernel32.GetTickCount() + 40
            user32.SetCursorPos(*self.park)
        self.events.put(("enter", frac))
        log("-> Mac")
        return True

    def go_local(self, frac=0.5, notify_client=True):
        with self._lock:
            if not self.remote:
                return
            self.remote = False
            l, t, r, b = self.edge_mon or monitor_rect(*self.park)
            frac = min(max(frac, 0.0), 1.0)
            if self.side == "right":
                x, y = r - 3, t + int(frac * (b - t))
            elif self.side == "left":
                x, y = l + 2, t + int(frac * (b - t))
            elif self.side == "bottom":
                x, y = l + int(frac * (r - l)), b - 3
            else:
                x, y = l + int(frac * (r - l)), t + 2
            user32.SetCursorPos(x, y)
        if notify_client:
            self.events.put(("leave",))
        log("<- PC")

    # ------------------------------------------------------------- hooks

    def _mouse_proc(self, nCode, wParam, lParam):
        if nCode < 0:
            return user32.CallNextHookEx(None, nCode, wParam, lParam)
        ms = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
        if ms.flags & LLMHF_INJECTED:
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        if not self.remote:
            if wParam in (WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN, WM_XBUTTONDOWN):
                self.local_buttons += 1
            elif wParam in (WM_LBUTTONUP, WM_RBUTTONUP, WM_MBUTTONUP, WM_XBUTTONUP):
                self.local_buttons = max(0, self.local_buttons - 1)
            elif wParam == WM_MOUSEMOVE and self.chan is not None and self.local_buttons == 0:
                hit = self.edge_hit(ms.pt.x, ms.pt.y)
                if hit and self.go_remote(*hit):
                    return 1
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        # ---- remote: swallow everything and forward it
        if wParam == WM_MOUSEMOVE:
            dx, dy = ms.pt.x - self.park[0], ms.pt.y - self.park[1]
            if (dx or dy) and ms.time >= self.ignore_moves_until:
                self.events.put(("move", dx, dy))
        elif wParam in (WM_LBUTTONDOWN, WM_LBUTTONUP):
            self.events.put(("btn", "left", wParam == WM_LBUTTONDOWN))
        elif wParam in (WM_RBUTTONDOWN, WM_RBUTTONUP):
            self.events.put(("btn", "right", wParam == WM_RBUTTONDOWN))
        elif wParam in (WM_MBUTTONDOWN, WM_MBUTTONUP):
            self.events.put(("btn", "middle", wParam == WM_MBUTTONDOWN))
        elif wParam in (WM_XBUTTONDOWN, WM_XBUTTONUP):
            which = "x1" if (ms.mouseData >> 16) & 0xFFFF == 1 else "x2"
            self.events.put(("btn", which, wParam == WM_XBUTTONDOWN))
        elif wParam in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
            delta = ctypes.c_short((ms.mouseData >> 16) & 0xFFFF).value
            if wParam == WM_MOUSEWHEEL:
                self.events.put(("wheel", 0, delta))
            else:
                self.events.put(("wheel", delta, 0))
        return 1

    def _hotkey_pressed(self, vk):
        if not self.hotkey:
            return False
        if not any(vk in s for s in self.hotkey):
            return False
        return all(self.pressed & s for s in self.hotkey)

    def _kb_proc(self, nCode, wParam, lParam):
        if nCode < 0:
            return user32.CallNextHookEx(None, nCode, wParam, lParam)
        kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        if kb.flags & LLKHF_INJECTED:
            return user32.CallNextHookEx(None, nCode, wParam, lParam)
        vk = kb.vkCode
        down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
        repeat = down and vk in self.pressed
        if down:
            self.pressed.add(vk)
        else:
            self.pressed.discard(vk)

        if down and not repeat and self._hotkey_pressed(vk):
            if self.remote:
                self.go_local(0.5)
            elif self.chan is not None:
                l, t, r, b = monitor_rect(*self._cursor())
                self.go_remote(0.5, (l, t, r, b))
            self.local_down.discard(vk)
            return 1

        if not self.remote:
            if down:
                self.local_down.add(vk)
            else:
                self.local_down.discard(vk)
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        if not down and vk in self.local_down:
            # key was held before we switched: let Windows see it released
            self.local_down.discard(vk)
            return user32.CallNextHookEx(None, nCode, wParam, lParam)
        self.events.put(("key", vk, bool(kb.flags & LLKHF_EXTENDED), down))
        return 1

    @staticmethod
    def _cursor():
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def hook_loop(self):
        self.hook_thread_id = kernel32.GetCurrentThreadId()
        self._mouse_cb = HOOKPROC(self._mouse_proc)
        self._kb_cb = HOOKPROC(self._kb_proc)
        hmod = kernel32.GetModuleHandleW(None)
        mh = user32.SetWindowsHookExW(WH_MOUSE_LL, self._mouse_cb, hmod, 0)
        kh = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_cb, hmod, 0)
        if not mh or not kh:
            log("Failed to install input hooks, error", ctypes.get_last_error())
            return
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnhookWindowsHookEx(mh)
        user32.UnhookWindowsHookEx(kh)

    # ------------------------------------------------------------- network

    def _send(self, obj):
        chan = self.chan
        if chan is None:
            return
        try:
            chan.send(obj)
        except OSError as e:
            log("Send failed:", e)
            self._drop(chan)

    def _drop(self, chan):
        with self._lock:
            if self.chan is chan:
                self.chan = None
                if self.remote:
                    self.go_local(0.5, notify_client=False)
        try:
            chan.close()
        except OSError:
            pass

    def sender_loop(self):
        """Turns raw hook events into protocol messages (keeps the hook callbacks fast)."""
        last_ping = 0.0
        while True:
            try:
                ev = self.events.get(timeout=1.0)
            except queue.Empty:
                ev = None
            now = time.monotonic()
            if now - last_ping > 2.0:
                self._send({"t": "ping"})
                last_ping = now
            if ev is None:
                continue
            kind = ev[0]
            if kind == "move":
                dx, dy = ev[1], ev[2]
                # coalesce queued moves so a 1000 Hz mouse doesn't flood the link
                while True:
                    try:
                        nxt = self.events.get_nowait()
                    except queue.Empty:
                        nxt = None
                        break
                    if nxt[0] != "move":
                        break
                    dx += nxt[1]
                    dy += nxt[2]
                self._send({"t": "move", "dx": dx, "dy": dy})
                if nxt is not None:
                    self._handle_other(nxt)
            else:
                self._handle_other(ev)

    def _handle_other(self, ev):
        kind = ev[0]
        if kind == "enter":
            if self.cfg.get("clipboard_sync", True):
                text = get_clipboard_text()
                if text and text != self.last_clip and len(text) < 1_000_000:
                    self.last_clip = text
                    self._send({"t": "clip", "text": text})
            self._send({"t": "enter", "side": self.side, "pos": ev[1]})
        elif kind == "leave":
            self._send({"t": "leave"})
        elif kind == "btn":
            self._send({"t": "btn", "b": ev[1], "down": ev[2]})
        elif kind == "wheel":
            self._send({"t": "wheel", "dx": ev[1], "dy": ev[2]})
        elif kind == "key":
            vk, ext, down = ev[1], ev[2], ev[3]
            if vk in common.MEDIA_KEYS:
                if down:
                    self._send({"t": "media", "k": common.MEDIA_KEYS[vk]})
                return
            code = common.map_key(self.keymap, vk, ext)
            if code is not None:
                self._send({"t": "key", "code": code, "down": down})

    def client_session(self, sock, addr):
        try:
            sock.settimeout(10)
            chan = common.SecureChannel.handshake(sock, self.cfg["passphrase"], is_server=True)
            hello = chan.recv()
            if hello.get("t") != "hello":
                raise ConnectionError("bad hello")
        except Exception as e:
            log(f"Rejected {addr[0]}: {e}")
            sock.close()
            return
        sock.settimeout(8)  # client pings every 2s
        with self._lock:
            old = self.chan
            self.chan = chan
        if old is not None:
            self._drop(old)
        log(f"Mac connected from {addr[0]} ({hello.get('name', '?')}, "
            f"screen {hello.get('w')}x{hello.get('h')})")
        try:
            while True:
                msg = chan.recv()
                t = msg.get("t")
                if t == "leave":
                    self.go_local(float(msg.get("pos", 0.5)), notify_client=False)
                elif t == "clip" and self.cfg.get("clipboard_sync", True):
                    text = msg.get("text") or ""
                    if text != self.last_clip:
                        self.last_clip = text
                        set_clipboard_text(text)
        except (OSError, ConnectionError, ValueError) as e:
            log(f"Mac disconnected: {e}")
        finally:
            self._drop(chan)

    def accept_loop(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.cfg["port"]))
        srv.listen(2)
        log(f"Listening on TCP {self.cfg['port']} (Mac is on the {self.side})")
        while True:
            sock, addr = srv.accept()
            threading.Thread(target=self.client_session, args=(sock, addr), daemon=True).start()

    def beacon_loop(self):
        tag = common.discovery_tag(self.cfg["passphrase"])
        payload = common.DISCOVERY_MAGIC + b"|" + tag + b"|" + str(self.cfg["port"]).encode()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        while True:
            if self.chan is None:
                try:
                    s.sendto(payload, ("255.255.255.255", self.cfg["port"] + 1))
                except OSError:
                    pass
            time.sleep(2)


def local_ips():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def main():
    if sys.platform != "win32":
        raise SystemExit("pc_server.py runs on Windows. On the Mac, run mac_client.py.")
    set_dpi_aware()
    cfg = common.load_config(create_if_missing=True)
    srv = Server(cfg)
    log("PC-Mac KVM server")
    log("This PC's addresses:", ", ".join(local_ips()) or "unknown")
    log("Toggle hotkey:", "+".join(cfg.get("hotkey", [])), "| Ctrl+C here to quit")
    for fn in (srv.accept_loop, srv.sender_loop, srv.beacon_loop):
        threading.Thread(target=fn, daemon=True).start()
    hook = threading.Thread(target=srv.hook_loop, daemon=True)
    hook.start()
    try:
        while hook.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if srv.remote:
            srv.go_local(0.5)
            time.sleep(0.1)
        if srv.hook_thread_id:
            user32.PostThreadMessageW(srv.hook_thread_id, WM_QUIT, 0, 0)
        log("Bye")


if __name__ == "__main__":
    main()
