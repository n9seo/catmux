# catmux

**CAT serial multiplexer for amateur radio transceivers — Linux equivalent of LP-Bridge / LPB2**

Allows multiple applications (WSJT-X, Fldigi, loggers, LinBPQ, etc.) to share a single CAT/CI-V serial port simultaneously, without any of them knowing they're not talking directly to the radio.

Inspired by [LP-Bridge / LPB2](http://www.telepostinc.com/LPB2.html) by Larry N8LP (TelePost Inc.).

---

## Supported Rigs

| Family | Rigs | Protocol |
|--------|------|----------|
| `yaesu` | FT-991A, FT-dx101, FT-710, FT-450, FT-818, FT-991 | ASCII `;` terminated |
| `elecraft` | KX3, KX2, K3, K3S, K4 | ASCII `;` terminated (Kenwood heritage) |
| `kenwood` | TS-590, TS-2000, TS-890, TS-990 | ASCII `;` terminated |
| `icom` | IC-7100, IC-7300, IC-7600, IC-9700 | Binary CI-V (`FE FE...FD`) |

---

## How it Works

```
         Radio (/dev/ttyUSB0)
                │
           [catmux broker]
          ┌─────┤─────────────┐
          │  Mirror Cache     │  ← answers GET queries instantly
          │  Command Queue    │  ← serializes SET commands, FIFO
          │  Signal Forward   │  ← RTS/DTR ↔ PTT/CW, CTS/DSR mirrored
          └─────┬─────────────┘
       ┌────────┼────────┐
  /dev/catmux/vport0  vport1  vport2 ...
       │        │        │
    WSJT-X   Fldigi  LinBPQ/Logger
```

**Mirror Cache:** Most traffic from logging apps is frequency/mode polling (FA; FB; MD;). catmux intercepts these, answers from its cached state snapshot, and only forwards them to the radio when the cache goes stale. This eliminates the collision problem entirely.

**Command Queue:** SET commands and cache misses are queued (priority queue — PTT always first) and dispatched to the radio one at a time. Responses are routed back to the correct virtual port.

**Signal Forwarding:**
- RTS from a virtual port → forwarded to real port RTS (PTT / CW key)
- DTR from a virtual port → forwarded to real port DTR (CW key)
- CTS/DSR from real port → mirrored to all virtual ports
- **Icom exception:** IC-7100 ignores RTS/DTR on USB. catmux automatically translates RTS changes into CI-V PTT commands (0x1C).

---

## Installation

```bash
# Clone
git clone https://github.com/yourhandle/catmux
cd catmux

# Install dependencies
pip install pyserial tomli  # tomli only needed for Python < 3.11

# Optional: install as command
pip install -e .
```

### Linux permissions for serial ports

```bash
# Add your user to the dialout group (log out/in after)
sudo usermod -aG dialout $USER

# Or for the specific port
sudo chmod a+rw /dev/ttyUSB0
```

### tty0tty kernel module (recommended)

While catmux uses `os.openpty()` for virtual ports, installing `tty0tty` gives better signal fidelity if you need kernel-level TIOCM support:

```bash
git clone https://github.com/lcgamboa/tty0tty
cd tty0tty/module && make && sudo insmod tty0tty.ko
```

---

## Configuration

Copy and edit one of the example configs from `config/`:

```bash
cp config/ft991a.toml catmux.toml   # for FT-991A
cp config/kx3.toml    catmux.toml   # for KX3
cp config/ic7100.toml catmux.toml   # for IC-7100
```

### Key settings

```toml
[rig]
family  = "yaesu"          # yaesu | elecraft | kenwood | icom
port    = "/dev/ttyUSB0"   # find with: ls /dev/serial/by-id/
baud    = 38400            # must match radio's CAT/CI-V baud setting
rts_on_connect = true      # FT-991A requires this; others usually false

[[vports.ports]]
name     = "wsjt-x"        # label (informational)
priority = 10              # lower = higher priority
```

### Finding your CAT port

```bash
ls -la /dev/serial/by-id/
# Look for entries like:
# usb-Yaesu_FT-991A_... -> ../../ttyUSB0   (CAT port)
# usb-Yaesu_FT-991A_... -> ../../ttyUSB1   (audio - ignore this one)
```

---

## Usage

```bash
# Start with default config (catmux.toml in current dir)
python catmux_main.py

# Specify config file
python catmux_main.py --config config/ft991a.toml

# Show mirror status every 5 seconds
python catmux_main.py --status

# Debug logging (shows every frame)
python catmux_main.py --debug
```

catmux creates symlinks under `/dev/catmux/`:

```
/dev/catmux/vport0  →  /dev/pts/3   (wsjt-x)
/dev/catmux/vport1  →  /dev/pts/4   (logger)
/dev/catmux/vport2  →  /dev/pts/5   (fldigi)
```

Point your apps at `/dev/catmux/vport0` etc. Set the same baud rate as your radio in each app.

---

## Rig-specific notes

### Yaesu FT-991A
- Menu **031 CAT RATE**: set to 38400
- Menu **032 CAT TOT**: set to 10ms or Off
- Menu **033 CAT RTS**: Off (catmux manages RTS)
- `rts_on_connect = true` is required — the FT-991A won't respond to CAT over USB unless RTS is asserted

### Elecraft KX3
- `CONFIG > RS232`: set baud to match config
- `CONFIG > PTT-KEY`: configures which RS-232 line does PTT vs CW key
- **Important:** don't assert RTS/DTR while in the PTT-KEY menu or the radio enters TEST mode (zero power)

### Icom IC-7100
- Menu **CI-V Baud Rate**: set to match config (default 19200)
- Menu **CI-V Address**: default 0x88; update `civ_address` if changed
- Only `/dev/ttyUSB0` is the CAT port; `/dev/ttyUSB1` is RTTY/GPS
- PTT via apps works normally — catmux translates RTS→CI-V automatically
- CW keying via CI-V is not yet implemented (contributions welcome)

---

## Running as a systemd service

```ini
# /etc/systemd/system/catmux.service
[Unit]
Description=catmux CAT serial multiplexer
After=network.target

[Service]
Type=simple
User=your_username
WorkingDirectory=/home/your_username/catmux
ExecStart=/usr/bin/python3 catmux_main.py --config /home/your_username/catmux/catmux.toml
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable catmux
sudo systemctl start catmux
sudo systemctl status catmux
```

---

## Architecture

```
catmux/
├── framer.py    Protocol framers (SemicolonFramer, CIVFramer)
├── mirror.py    State cache + poll scheduler
├── vport.py     PTY pair management + TIOCM signal handling
├── broker.py    Central arbitrator (owns serial port, command queue)
└── __init__.py

config/
├── ft991a.toml  Yaesu FT-991A example
├── kx3.toml     Elecraft KX3 example
└── ic7100.toml  Icom IC-7100 example

catmux_main.py   CLI entry point
```

---

## Roadmap

- [ ] Web status dashboard (Flask/FastAPI)
- [ ] Per-port command filtering (block AI mode changes, etc.)
- [ ] Output ports for SteppIR / amplifier band data
- [ ] Icom CI-V CW keying
- [ ] Auto-reconnect on USB disconnect/reconnect
- [ ] Windows support (com0com virtual ports)
- [ ] More rig profiles (Kenwood TS-890, IC-7300, etc.)

---

## License

CATMUX AMATEUR RADIO LICENSE — contributions welcome. If you improve catmux, please share back with the ham radio community.

73 de N9SEO

---

## Serial port permissions

The group that owns `/dev/ttyUSB*` varies by distro. Run the included helper
to detect and fix it automatically:

```bash
bash setup_permissions.sh
```

What it does under the hood:

```bash
# Find out which group owns your serial device
stat -c '%G' /dev/ttyUSB0
# → "uucp" on Arch/Manjaro, "dialout" on Debian/Ubuntu/Fedora

# Add yourself to that group
sudo usermod -aG uucp $USER      # Arch/Manjaro
sudo usermod -aG dialout $USER   # Debian/Ubuntu/Fedora

# Take effect immediately without logging out
newgrp uucp
```

If you prefer not to use a group, you can also just:

```bash
sudo chmod a+rw /dev/ttyUSB0
```
Though this resets on replug — the group approach is permanent.
