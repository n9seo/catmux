# catmux Configuration Guide

catmux is configured via a single [TOML](https://toml.io) file, typically named `catmux.toml` in your working directory. You can specify a different file with `--config`.

---

## Table of Contents

- [Quick Start](#quick-start)
- [Rig Section](#rig-section)
- [Virtual Ports Section](#virtual-ports-section)
- [PTT Routing](#ptt-routing)
- [Command Suppression](#command-suppression)
- [Device Mode (tty0tty)](#device-mode-tty0tty)
- [Logging Section](#logging-section)
- [Full Example Configs](#full-example-configs)
- [Finding Your Serial Port](#finding-your-serial-port)
- [Rig-Specific Notes](#rig-specific-notes)

---

## Quick Start

```bash
# Copy the example config for your radio
cp config/ft991a.toml catmux.toml

# Edit the port to match your radio
nano catmux.toml

# Run
python3 catmux_main.py
```

---

## Rig Section

The `[rig]` section defines your transceiver connection.

```toml
[rig]
family           = "yaesu"        # Protocol family (see below)
port             = "/dev/FT991A_CAT"  # Serial port device
baud             = 38400          # Baud rate — must match radio menu setting
mirror_ttl       = 1.0            # Seconds before cached values go stale
response_timeout = 0.2            # Seconds to wait for GET response from radio
set_timeout      = 0.05           # Seconds to wait after SET command (most are silent)
rts_on_connect   = true           # Assert RTS on connect (required for Yaesu USB CAT)
serial_group     = "uucp"         # Serial port group (uucp on Arch/Manjaro, dialout on Debian/Ubuntu)
```

### `family` — Protocol Family

| Value | Radios | Protocol |
|-------|--------|----------|
| `yaesu` | FT-991A, FT-dx101, FT-710, FT-450, FT-991 | ASCII `;` terminated |
| `elecraft` | KX3, KX2, K3, K3S, K4 | ASCII `;` terminated (Kenwood heritage) |
| `kenwood` | TS-590, TS-2000, TS-890, TS-990 | ASCII `;` terminated |
| `icom` | IC-7100, IC-7300, IC-7600 | Binary CI-V (`FE FE ... FD`) |

### `rts_on_connect`

| Radio | Setting |
|-------|---------|
| Yaesu FT-991A | `true` — USB CAT won't respond without RTS asserted |
| Elecraft KX3 | `false` |
| Kenwood | `false` |
| Icom | `false` — CI-V ignores RTS/DTR |

### `serial_group`

The group that owns `/dev/ttyUSB*` varies by Linux distribution:

| Distribution | Group |
|-------------|-------|
| Arch / Manjaro | `uucp` |
| Debian / Ubuntu / Mint | `dialout` |
| Fedora / RHEL / CentOS | `dialout` |
| openSUSE | `dialout` |

### Icom CI-V Additional Settings

```toml
[rig]
family           = "icom"
civ_address      = "0x88"   # IC-7100 default — check Menu > CI-V Address
civ_ctrl_address = "0xE0"   # catmux controller address (any unused CI-V address)
```

---

## Virtual Ports Section

The `[vports]` section defines where catmux creates virtual serial port symlinks and how many ports to create.

```toml
[vports]
symlink_dir = "/home/username/.catmux"  # Where vport symlinks are created
                                         # Use a home directory path to avoid
                                         # needing root. /dev/catmux requires
                                         # sudo or a tmpfiles.d rule.

  [[vports.ports]]
  name     = "wsjt-x"    # Label for logging — pick anything descriptive
  priority = 10          # Lower = higher priority in command queue
  ptt_from = "cat"       # How the app signals PTT (rts | dtr | cat)
  ptt_via  = "cat"       # How catmux keys the radio (rts | dtr | cat)
  suppress = ["SH", "NA", "EX"]  # SET commands to silently drop (optional)
```

### Priority Values

| Priority | Meaning |
|----------|---------|
| `0` | PTT commands (reserved, set automatically) |
| `5` | SET commands (freq/mode changes) |
| `10` | Normal app (WSJT-X, Fldigi) |
| `15` | Logger (slightly lower priority) |
| `20` | Background apps (LinBPQ) |
| `25` | Low priority / Wine apps |

Lower numbers win. Within the same priority, commands are served FIFO.

---

## PTT Routing

PTT routing is configured per virtual port with two settings:

```toml
ptt_from = "rts"   # Signal the APP uses for PTT
ptt_via  = "cat"   # Signal catmux uses to key the RADIO
```

### `ptt_from` — How the app signals PTT

| Value | Meaning |
|-------|---------|
| `rts` | App asserts the RTS line (most apps default) |
| `dtr` | App asserts the DTR line (Direwolf, some TNCs) |
| `cat` | App sends `TX1;`/`TX0;` CAT commands directly (WSJT-X CAT PTT mode) |

### `ptt_via` — How catmux keys the radio

| Value | Meaning |
|-------|---------|
| `rts` | catmux asserts real port RTS — fastest, direct hardware |
| `dtr` | catmux asserts real port DTR |
| `cat` | catmux sends `TX1;`/`TX0;` (Yaesu/Kenwood/Elecraft) or CI-V 0x1C (Icom) |

### Common PTT Configurations

```toml
# WSJT-X with CAT PTT
ptt_from = "cat"
ptt_via  = "cat"

# Fldigi with RTS PTT, radio accepts hardware RTS
ptt_from = "rts"
ptt_via  = "rts"

# Fldigi with RTS PTT, use CAT command instead of hardware
ptt_from = "rts"
ptt_via  = "cat"

# Direwolf / WinAPRS using DTR for PTT
ptt_from = "dtr"
ptt_via  = "cat"

# KX3 — hardware RTS not always available, use CAT
ptt_from = "rts"
ptt_via  = "cat"
```

### PTT Latency

catmux is optimised for minimum PTT latency:

- **RTS/DTR signal changes** are detected by polling at 10ms intervals
- **CAT PTT commands** (`TX1;`/`TX0;`) bypass the command queue entirely — written directly to the serial port, preempting any in-flight transaction
- **Icom CI-V PTT** goes through the priority queue at `PRI_PTT=0`

---

## Command Suppression

Some applications (WSJT-X, flrig, loggers using hamlib) send SET commands that override radio settings you may want to keep. The `suppress` list silently drops these before they reach the radio.

```toml
[[vports.ports]]
name     = "wsjt-x"
suppress = ["SH", "NA", "EX"]
```

### Common Commands to Suppress

| Command | Meaning | Why suppress |
|---------|---------|--------------|
| `SH` | IF filter width | hamlib sets this to narrow on connect, overriding your setting |
| `NA` | Noise reduction | hamlib turns this off, overriding your setting |
| `EX` | Menu/extension settings | hamlib touches radio menu items during polling |
| `KS` | Keyer speed | Suppress if you don't want logger controlling CW speed |

Suppressed commands are dropped silently — the app never knows they were blocked.

---

## Device Mode (tty0tty)

For Windows applications running under Wine that require full serial port ioctl support (flow control, handshake lines), use the `device` option to connect a virtual port directly to a `tty0tty` null-modem pair instead of a PTY.

```toml
[[vports.ports]]
name     = "winapp"
priority = 25
ptt_from = "rts"
ptt_via  = "cat"
device   = "/dev/tnt0"    # catmux uses this end
```

Then point Wine at the other end:

```bash
wine reg add 'HKLM\Software\Wine\Ports' /v COM4 /t REG_SZ /d '/dev/tnt1'
```

### Installing tty0tty

```bash
git clone https://github.com/lcgamboa/tty0tty
cd tty0tty/module
make
sudo insmod tty0tty.ko

# Persist across reboots
sudo cp tty0tty.ko /lib/modules/$(uname -r)/kernel/drivers/tty/
sudo depmod -a
echo 'tty0tty' | sudo tee /etc/modules-load.d/tty0tty.conf
echo 'KERNEL=="tnt[0-9]*", MODE="0666"' | sudo tee /etc/udev/rules.d/99-tty0tty.rules
sudo udevadm control --reload-rules
```

---

## Logging Section

```toml
[log]
level = "INFO"    # DEBUG | INFO | WARNING | ERROR
file  = ""        # Path to log file, or empty for stdout only
```

Set `level = "DEBUG"` to see every CAT frame sent and received — useful for diagnosing connection issues. Pipe to a file for later analysis:

```bash
python3 catmux_main.py --debug 2>&1 | tee /tmp/catmux_debug.log
```

---

## Full Example Configs

### Yaesu FT-991A — Multiple Apps

```toml
[rig]
family           = "yaesu"
port             = "/dev/FT991A_CAT"
baud             = 38400
mirror_ttl       = 1.0
response_timeout = 0.2
set_timeout      = 0.05
rts_on_connect   = true
serial_group     = "uucp"

[vports]
symlink_dir = "/home/username/.catmux"

  [[vports.ports]]
  name     = "wsjt-x"
  priority = 10
  ptt_from = "cat"
  ptt_via  = "cat"
  suppress = ["SH", "NA", "EX"]

  [[vports.ports]]
  name     = "flrig"
  priority = 15
  ptt_from = "rts"
  ptt_via  = "cat"
  suppress = ["SH", "NA"]

  [[vports.ports]]
  name     = "qlog"
  priority = 10
  ptt_from = "rts"
  ptt_via  = "rts"
  suppress = ["SH", "NA", "EX"]

  [[vports.ports]]
  name     = "linbpq"
  priority = 20
  ptt_from = "rts"
  ptt_via  = "cat"

  [[vports.ports]]
  name     = "winrpr"
  priority = 25
  ptt_from = "rts"
  ptt_via  = "cat"
  device   = "/dev/tnt0"

[log]
level = "INFO"
file  = ""
```

### Elecraft KX3

```toml
[rig]
family           = "elecraft"
port             = "/dev/KX3_CAT"
baud             = 38400
mirror_ttl       = 1.0
response_timeout = 0.2
set_timeout      = 0.05
rts_on_connect   = false
serial_group     = "uucp"

[vports]
symlink_dir = "/home/username/.catmux"

  [[vports.ports]]
  name     = "wsjt-x"
  priority = 10
  ptt_from = "rts"
  ptt_via  = "cat"

  [[vports.ports]]
  name     = "logger"
  priority = 15
  ptt_from = "rts"
  ptt_via  = "rts"

[log]
level = "INFO"
file  = ""
```

### Icom IC-7100

```toml
[rig]
family           = "icom"
port             = "/dev/IC7100_CAT"
baud             = 19200
civ_address      = "0x88"
civ_ctrl_address = "0xE0"
mirror_ttl       = 1.0
response_timeout = 1.0
set_timeout      = 0.1
rts_on_connect   = false
serial_group     = "uucp"

[vports]
symlink_dir = "/home/username/.catmux"

  [[vports.ports]]
  name     = "wsjt-x"
  priority = 10
  ptt_from = "rts"
  ptt_via  = "cat"    # IC-7100 USB ignores RTS — catmux translates to CI-V

  [[vports.ports]]
  name     = "logger"
  priority = 15
  ptt_from = "rts"
  ptt_via  = "cat"

[log]
level = "INFO"
file  = ""
```

---

## Finding Your Serial Port

Using udev symlinks (recommended — port name is stable across reboots):

```bash
ls -la /dev/serial/by-id/
```

You will see entries like:

```
usb-Silicon_Labs_CP2105_..._01CF0CAE-if00-port0 -> ../../ttyUSB1   ← CAT port
usb-Silicon_Labs_CP2105_..._01CF0CAE-if01-port0 -> ../../ttyUSB2   ← audio/data
```

Use the `by-id` path directly, or create a named udev rule (see `docs/udev.md`).

---

## Rig-Specific Notes

### Yaesu FT-991A

- **Menu 031 CAT RATE** — set to match `baud` in config (38400 recommended)
- **Menu 032 CAT TOT** — set to `10ms` or `Off`
- **Menu 033 CAT RTS** — set to `Off` (catmux manages RTS)
- `rts_on_connect = true` is mandatory — the radio will not respond to CAT over USB without RTS asserted
- The FT-991A presents two USB serial ports — use interface `if00` (CAT), not `if01` (audio)

### Elecraft KX3

- **CONFIG > RS232** — set baud to match config
- **CONFIG > PTT-KEY** — configures which RS-232 line is PTT vs CW key
- Do not have the radio in the PTT-KEY menu when catmux starts — asserting RTS/DTR while in that menu puts the radio into TEST mode (zero power output)

### Icom IC-7100

- **CI-V Baud Rate menu** — set to match `baud` in config (default 19200)
- **CI-V Address menu** — default `0x88`, update `civ_address` if changed
- The IC-7100 USB firmware physically ignores RTS and DTR — catmux automatically translates RTS PTT signals to CI-V command `0x1C`
- Only the first USB serial port is CAT — the second is RTTY/GPS data
