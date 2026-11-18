"""Shared bits for the PC <-> Mac software KVM: config, encrypted framing, keymap.

Wire format (after handshake), per message:
    4-byte big-endian length | AES-256-GCM ciphertext of a UTF-8 JSON object

Handshake (plaintext, before anything else):
    server -> client : MAGIC + 16 random bytes (server nonce)
    client -> server : MAGIC + 16 random bytes (client nonce)
Both sides derive two directional keys from the shared passphrase and both nonces.
The first encrypted message from the client must be {"t": "hello", ...}; if the
server can't decrypt it, the passphrase is wrong and the connection is dropped.
"""

import hashlib
import hmac
import json
import os
import secrets
import socket
import struct
import threading

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

MAGIC = b"PCMACKVM1"
DISCOVERY_MAGIC = b"PCMACKVM-HERE"
MAX_FRAME = 4 * 1024 * 1024

DEFAULT_CONFIG = {
    # Shared secret. Generated on first run of the PC server; copy config.json to the Mac.
    "passphrase": "",
    # TCP port the PC listens on; UDP discovery beacon uses port + 1.
    "port": 24800,
    # Where the Mac sits relative to the PC screen(s): "left" or "right" (also "top"/"bottom").
    "mac_side": "right",
    # Mac only: PC address. Leave empty to auto-discover over the LAN.
    "pc_host": "",
    # Don't switch when the cursor hits the edge within this many px of a corner
    # (keeps the window close button in the top-right corner usable).
    "corner_dead_zone_px": 40,
    # PC keyboard -> Mac modifier mapping. Default: Win->Cmd, Alt->Option, Ctrl->Control.
    # Set true to make Alt act as Cmd and Win act as Option (closer to the Mac layout).
    "swap_alt_cmd": False,
    # Multiplier applied to mouse movement on the Mac (tune if the cursor feels slow/fast).
    "mouse_speed": 1.0,
    # Flip scroll direction on the Mac, and how many lines one wheel notch scrolls.
    "invert_scroll": False,
    "scroll_lines_per_notch": 3,
    # Sync plain-text clipboard when the cursor crosses over.
    "clipboard_sync": True,
    # Keys that, pressed together, toggle control between PC and Mac (Windows VK names).
    "hotkey": ["CTRL", "ALT", "SPACE"],
}


def load_config(create_if_missing=False):
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    elif not create_if_missing:
        raise SystemExit(
            f"Missing {CONFIG_PATH}. Run the PC server once to generate it, "
            "then copy config.json next to mac_client.py on the Mac."
        )
    if not cfg.get("passphrase"):
        if not create_if_missing:
            raise SystemExit("config.json has an empty passphrase. Copy it from the PC.")
        cfg["passphrase"] = secrets.token_urlsafe(18)
        save_config(cfg)
        print(f"Created {CONFIG_PATH} with a new random passphrase.")
    elif not os.path.exists(CONFIG_PATH):
        save_config(cfg)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


# --------------------------------------------------------------------------- crypto

def _psk(passphrase):
    # cryptography's Scrypt, not hashlib.scrypt: Apple's bundled Python is built
    # against LibreSSL and has no hashlib.scrypt. Output is identical.
    return Scrypt(salt=b"pcmac-kvm-v1", length=32, n=2**14, r=8, p=1).derive(
        passphrase.encode("utf-8")
    )


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return bytes(buf)


class SecureChannel:
    """Encrypted, authenticated, ordered JSON message channel over a TCP socket."""

    def __init__(self, sock, send_key, recv_key):
        self.sock = sock
        self._send = AESGCM(send_key)
        self._recv = AESGCM(recv_key)
        self._send_ctr = 0
        self._recv_ctr = 0
        self._lock = threading.Lock()

    @classmethod
    def handshake(cls, sock, passphrase, is_server):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        mine = secrets.token_bytes(16)
        if is_server:
            sock.sendall(MAGIC + mine)
            hdr = _recv_exact(sock, len(MAGIC) + 16)
            ns, nc = mine, hdr[len(MAGIC):]
        else:
            hdr = _recv_exact(sock, len(MAGIC) + 16)
            sock.sendall(MAGIC + mine)
            ns, nc = hdr[len(MAGIC):], mine
        if hdr[: len(MAGIC)] != MAGIC:
            raise ConnectionError("peer is not a PC-Mac KVM endpoint")
        psk = _psk(passphrase)
        k_s2c = hmac.new(psk, b"s2c" + ns + nc, hashlib.sha256).digest()
        k_c2s = hmac.new(psk, b"c2s" + ns + nc, hashlib.sha256).digest()
        if is_server:
            return cls(sock, k_s2c, k_c2s)
        return cls(sock, k_c2s, k_s2c)

    def send(self, obj):
        data = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        with self._lock:
            nonce = self._send_ctr.to_bytes(12, "big")
            self._send_ctr += 1
            ct = self._send.encrypt(nonce, data, None)
            self.sock.sendall(struct.pack(">I", len(ct)) + ct)

    def recv(self):
        (n,) = struct.unpack(">I", _recv_exact(self.sock, 4))
        if n > MAX_FRAME:
            raise ConnectionError("frame too large")
        ct = _recv_exact(self.sock, n)
        nonce = self._recv_ctr.to_bytes(12, "big")
        self._recv_ctr += 1
        try:
            data = self._recv.decrypt(nonce, ct, None)
        except Exception:
            raise ConnectionError("decryption failed (wrong passphrase?)") from None
        return json.loads(data.decode("utf-8"))

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


def discovery_tag(passphrase):
    """Short tag in the discovery beacon so clients only pick up *their* PC."""
    return hashlib.sha256(b"discover" + passphrase.encode("utf-8")).hexdigest()[:16].encode()


# --------------------------------------------------------------------------- keymap
# Windows virtual-key code -> macOS virtual keycode (ANSI positional layout).

VK = {
    "BACK": 0x08, "TAB": 0x09, "RETURN": 0x0D, "SHIFT": 0x10, "CTRL": 0x11, "ALT": 0x12,
    "PAUSE": 0x13, "CAPITAL": 0x14, "ESCAPE": 0x1B, "SPACE": 0x20, "PRIOR": 0x21,
    "NEXT": 0x22, "END": 0x23, "HOME": 0x24, "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27,
    "DOWN": 0x28, "SNAPSHOT": 0x2C, "INSERT": 0x2D, "DELETE": 0x2E, "LWIN": 0x5B,
    "RWIN": 0x5C, "APPS": 0x5D, "NUMLOCK": 0x90, "SCROLL": 0x91,
    "LSHIFT": 0xA0, "RSHIFT": 0xA1, "LCTRL": 0xA2, "RCTRL": 0xA3, "LALT": 0xA4, "RALT": 0xA5,
}
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
    VK[_c] = ord(_c)
for _i in range(1, 25):
    VK[f"F{_i}"] = 0x6F + _i

# Groups used for hotkey matching ("CTRL" matches either Ctrl key, etc.).
VK_GROUPS = {
    "CTRL": {0x11, 0xA2, 0xA3},
    "SHIFT": {0x10, 0xA0, 0xA1},
    "ALT": {0x12, 0xA4, 0xA5},
    "WIN": {0x5B, 0x5C},
}

MAC_SHIFT, MAC_RSHIFT = 0x38, 0x3C
MAC_CTRL, MAC_RCTRL = 0x3B, 0x3E
MAC_OPT, MAC_ROPT = 0x3A, 0x3D
MAC_CMD, MAC_RCMD = 0x37, 0x36
MAC_CAPS = 0x39

_BASE_MAP = {
    # letters
    0x41: 0x00, 0x53: 0x01, 0x44: 0x02, 0x46: 0x03, 0x48: 0x04, 0x47: 0x05, 0x5A: 0x06,
    0x58: 0x07, 0x43: 0x08, 0x56: 0x09, 0x42: 0x0B, 0x51: 0x0C, 0x57: 0x0D, 0x45: 0x0E,
    0x52: 0x0F, 0x59: 0x10, 0x54: 0x11, 0x4F: 0x1F, 0x55: 0x20, 0x49: 0x22, 0x50: 0x23,
    0x4C: 0x25, 0x4A: 0x26, 0x4B: 0x28, 0x4E: 0x2D, 0x4D: 0x2E,
    # digits row
    0x31: 0x12, 0x32: 0x13, 0x33: 0x14, 0x34: 0x15, 0x36: 0x16, 0x35: 0x17, 0x39: 0x19,
    0x37: 0x1A, 0x38: 0x1C, 0x30: 0x1D,
    # punctuation (US layout OEM keys)
    0xBB: 0x18,  # = +
    0xBD: 0x1B,  # - _
    0xDD: 0x1E,  # ] }
    0xDB: 0x21,  # [ {
    0xDE: 0x27,  # ' "
    0xBA: 0x29,  # ; :
    0xDC: 0x2A,  # \ |
    0xBC: 0x2B,  # , <
    0xBF: 0x2C,  # / ?
    0xBE: 0x2F,  # . >
    0xC0: 0x32,  # ` ~
    0xE2: 0x0A,  # ISO extra key
    # editing / navigation
    0x0D: 0x24,  # Return (numpad Enter handled via extended flag)
    0x09: 0x30, 0x20: 0x31, 0x08: 0x33, 0x1B: 0x35, 0x2E: 0x75, 0x2D: 0x72,  # Insert->Help
    0x24: 0x73, 0x21: 0x74, 0x23: 0x77, 0x22: 0x79,
    0x25: 0x7B, 0x27: 0x7C, 0x28: 0x7D, 0x26: 0x7E,
    # modifiers
    0xA0: MAC_SHIFT, 0xA1: MAC_RSHIFT, 0x10: MAC_SHIFT,
    0xA2: MAC_CTRL, 0xA3: MAC_RCTRL, 0x11: MAC_CTRL,
    0x14: MAC_CAPS,
    # numpad
    0x60: 0x52, 0x61: 0x53, 0x62: 0x54, 0x63: 0x55, 0x64: 0x56, 0x65: 0x57, 0x66: 0x58,
    0x67: 0x59, 0x68: 0x5B, 0x69: 0x5C, 0x6A: 0x43, 0x6B: 0x45, 0x6D: 0x4E, 0x6E: 0x41,
    0x6F: 0x4B, 0x90: 0x47,  # NumLock -> Keypad Clear
    # function keys
    0x70: 0x7A, 0x71: 0x78, 0x72: 0x63, 0x73: 0x76, 0x74: 0x60, 0x75: 0x61, 0x76: 0x62,
    0x77: 0x64, 0x78: 0x65, 0x79: 0x6D, 0x7A: 0x67, 0x7B: 0x6F, 0x7C: 0x69, 0x7D: 0x6B,
    0x7E: 0x71, 0x7F: 0x6A, 0x80: 0x40, 0x81: 0x4F, 0x82: 0x50, 0x83: 0x5A,
}

# Media keys go over the wire as named "special" keys (macOS needs system-defined events).
MEDIA_KEYS = {0xAD: "mute", 0xAE: "volume_down", 0xAF: "volume_up",
              0xB0: "next", 0xB1: "previous", 0xB3: "play"}


def build_keymap(swap_alt_cmd=False):
    m = dict(_BASE_MAP)
    if swap_alt_cmd:
        m.update({0xA4: MAC_CMD, 0xA5: MAC_RCMD, 0x12: MAC_CMD,
                  0x5B: MAC_OPT, 0x5C: MAC_ROPT})
    else:
        m.update({0xA4: MAC_OPT, 0xA5: MAC_ROPT, 0x12: MAC_OPT,
                  0x5B: MAC_CMD, 0x5C: MAC_RCMD})
    m[0x5D] = MAC_RCMD if not swap_alt_cmd else MAC_ROPT  # context-menu key
    return m


def map_key(keymap, vk, extended):
    """Return a Mac keycode for a Windows VK (or None if unmapped)."""
    if vk == 0x0D and extended:
        return 0x4C  # numpad Enter
    return keymap.get(vk)
