# PIKMAK

**One keyboard and one mouse for your PC and your Mac, over Wi-Fi.**

PIKMAK (PC + Mac) treats a MacBook sitting next to your Windows monitor like an extra
display. Push the cursor off the edge of the PC screen and it shows up on the Mac. Type,
click and scroll there with the PC's keyboard and mouse. Push it back and you're on the PC
again. Text you copy on one machine pastes on the other.

```
 [ PC screen ]  -->  [ MacBook ]      mac_side = "right"
 [ MacBook ]  <--  [ PC screen ]      mac_side = "left"
```

No extra hardware, no account, no cloud. It's two small Python scripts and one shared
passphrase.

---

## How it started

I had a Bluetooth keyboard and mouse paired to my PC and a MacBook open right next to the
monitor on the same desk. Every time I needed the Mac, I reached for its trackpad and
typed on a second keyboard.

So I described the setup to [Claude Code](https://claude.com/claude-code) in **one
prompt**: "help me build a tool that allows me to move my mouse from one desktop to the
next as if they were extended displays of the same computer."

Claude wrote the PC server, the Mac client, the encrypted connection and the setup
scripts, and tested the PC side. When I ran it on the Mac it failed with one error. My
**second prompt** was a screenshot of that error. Claude fixed it, and both machines were
sharing one keyboard and mouse about **30 minutes** after the first message.

Two prompts. Two workstations, one set of hands.

---

## What it does

- **Edge switching.** Move off the Mac-facing edge of the PC screen to control the Mac.
  Move off the matching edge of the Mac screen to come back.
- **Full keyboard.** Shortcuts, modifiers, arrow keys, function keys and media keys
  (volume, play/pause, next track).
- **Clipboard sync.** Plain text follows the cursor across.
- **Encrypted.** Every keystroke is sent with AES-256-GCM, using a key derived from a
  shared passphrase, so your typing doesn't cross your Wi-Fi in the clear.
- **Auto-discovery.** The Mac finds the PC on the local network by itself.
- **Fails safe.** If the connection drops, control snaps back to the PC.

## How it works

| Machine | Script | Job |
|---|---|---|
| Windows PC | `pc_server.py` | Hooks the keyboard and mouse with low-level Windows hooks, watches for the screen edge, and forwards input while you're "on the Mac". |
| Mac | `mac_client.py` | Receives that input and replays it as native macOS events (Quartz). |
| Both | `common.py` | Config, the encrypted framing, discovery and the PC→Mac key map. |

The connection starts with a nonce exchange. Both sides then derive two directional
AES-256-GCM keys from the passphrase (scrypt) and the nonces. A wrong passphrase means the
first message can't be decrypted and the PC drops the connection.

---

## Requirements

- Windows 10 or 11 with Python 3.10+
- macOS with Python 3 (`xcode-select --install` gives you one)
- Both machines on the same local network

## Setup

### 1. PC

```powershell
git clone https://github.com/westernbridger/pikmak.git
cd pikmak
.\start_pc.bat
```

The first run creates `.venv`, installs `cryptography`, and writes `config.json` with a
random passphrase. Windows Firewall will ask about Python: allow it on **Private networks**.
The window prints this PC's IP addresses.

Open `config.json` and set `"mac_side"` to `"left"` or `"right"` to match your desk.

### 2. Mac

Copy the whole folder to the Mac, **including `config.json`** (AirDrop, USB stick, cloud
drive). Then in Terminal:

```bash
cd ~/path/to/pikmak
chmod +x start_mac.sh
./start_mac.sh                        # finds the PC on the network automatically
./start_mac.sh --host 192.168.1.20    # or give the PC's IP if discovery doesn't work
```

The first time, macOS asks for **Accessibility** permission. Open System Settings →
Privacy & Security → Accessibility, turn on **Terminal**, and run the script again.

When the Mac shows "Connected", move the mouse off the PC screen's edge.

> `config.json` holds your passphrase. It's in `.gitignore` so it never gets committed.
> Don't share it.

## Controls

| What | How |
|---|---|
| Go to the Mac | Push the cursor off the Mac-facing edge of the PC screen |
| Back to the PC | Push the cursor off the matching edge of the Mac screen |
| Force toggle | **Ctrl + Alt + Space** (change with `hotkey`) |
| Quit | Ctrl+C in the PC window |

Switching doesn't happen while a mouse button is held, or within `corner_dead_zone_px` of a
screen corner, so the close button of a maximized window still works.

## Keyboard mapping (PC keyboard → Mac)

| PC key | Mac key |
|---|---|
| Ctrl | Control |
| Win | ⌘ Command |
| Alt | ⌥ Option |

Set `"swap_alt_cmd": true` to make **Alt = ⌘** and **Win = ⌥**. That matches where ⌘ sits on
a Mac keyboard (next to the space bar), so Alt+C and Alt+V copy and paste.

## config.json

| Key | Default | Meaning |
|---|---|---|
| `passphrase` | random | Shared secret. Must match on both machines. |
| `port` | 24800 | TCP port on the PC (UDP discovery uses port + 1) |
| `mac_side` | `"right"` | `left` / `right` / `top` / `bottom` |
| `pc_host` | `""` | PC's IP for the Mac. Empty means auto-discover |
| `corner_dead_zone_px` | 40 | No switching this close to a corner |
| `swap_alt_cmd` | false | See keyboard mapping above |
| `mouse_speed` | 1.0 | Cursor speed multiplier on the Mac |
| `invert_scroll` | false | Flip scroll direction on the Mac |
| `scroll_lines_per_notch` | 3 | Scroll distance per wheel notch on the Mac |
| `clipboard_sync` | true | Sync plain-text clipboard on each crossing |
| `hotkey` | `["CTRL","ALT","SPACE"]` | Toggle keys (names from `VK` in `common.py`) |

## Known limits

- One PC driving one Mac. The PC is always the one with the keyboard and mouse.
- While you're on the Mac, the PC cursor waits in the middle of the PC screen.
- Ctrl+Alt+Del and the Windows lock screen can't be captured (Windows doesn't allow it).
- Typing uses US key positions. Other layouts mostly work if the Mac uses the same layout.
- Text clipboard only, no files or images.
- Multiple Mac displays are treated as one rectangle.

## Troubleshooting

- **Mac stuck on "Looking for the PC…"**: some networks block discovery broadcasts. Use
  `--host <PC IP>` or set `pc_host`.
- **"Rejected … decryption failed" on the PC**: the two `config.json` passphrases differ.
- **Connects but nothing happens on the Mac**: Accessibility permission is missing or was
  given to a different app. Remove Terminal from the list, add it again, and restart the
  script.
- **Testing without a Mac**: `python mac_client.py --dry-run --host 127.0.0.1` prints the
  events it would send.

## License

MIT. See [LICENSE](LICENSE).

---

Built by [Western Umbrella Corp](https://westernumbrella.com) with
[Claude Code](https://claude.com/claude-code).
