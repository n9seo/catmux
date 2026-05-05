# tty0tty — Null Modem for Wine/Windows Apps

Some Windows applications running under Wine require full serial port ioctl
support including hardware flow control signals (RTS/CTS, DTR/DSR). Standard
Linux PTY devices don't support these ioctls, causing Wine to fail to open
the port or show empty COM port dropdowns.

`tty0tty` is a kernel module that creates pairs of virtual serial ports
with full null-modem signal crossing — RTS on one end appears as CTS on the
other, DTR appears as DSR. This satisfies Wine's ioctl requirements.

---

## When You Need tty0tty

Use `tty0tty` when:
- A Windows app under Wine shows an empty COM port dropdown
- Wine logs show `IOCTL_SERIAL_GET_HANDFLOW` errors
- The app works on Windows but can't open the COM port under Wine

You do **not** need tty0tty for native Linux apps — PTY devices work fine.

---

## Installation

### Build from source

```bash
# Install kernel headers
sudo pacman -S linux-headers base-devel   # Arch/Manjaro
sudo apt install linux-headers-$(uname -r) build-essential  # Debian/Ubuntu

# Clone and build
git clone https://github.com/lcgamboa/tty0tty
cd tty0tty/module
make
sudo insmod tty0tty.ko

# Verify devices created
ls /dev/tnt*
# /dev/tnt0  /dev/tnt1  /dev/tnt2  /dev/tnt3 ...
```

### Persist across reboots

```bash
# Install module permanently
sudo cp tty0tty.ko /lib/modules/$(uname -r)/kernel/drivers/tty/
sudo depmod -a

# Auto-load on boot
echo 'tty0tty' | sudo tee /etc/modules-load.d/tty0tty.conf

# Set permissions via udev
echo 'KERNEL=="tnt[0-9]*", MODE="0666"' | sudo tee /etc/udev/rules.d/99-tty0tty.rules
sudo udevadm control --reload-rules
```

Or use the included systemd service:

```bash
sudo cp systemd/tty0tty.service /etc/systemd/system/
sudo systemctl enable tty0tty.service
sudo systemctl start tty0tty.service
```

---

## How It Works with catmux

tty0tty creates pairs of linked devices: `tnt0 ↔ tnt1`, `tnt2 ↔ tnt3`, etc.

```
Windows App → Wine COM4 → /dev/tnt1 ←null-modem→ /dev/tnt0 ← catmux ← Radio
```

catmux opens `tnt0` directly as the virtual port device. Wine points to `tnt1`.
Signal crossing means the app's RTS on `tnt1` appears as CTS on `tnt0` —
catmux detects this and fires the PTT callback.

---

## catmux Configuration

```toml
[[vports.ports]]
name     = "winapp"
priority = 25
ptt_from = "rts"
ptt_via  = "cat"
device   = "/dev/tnt0"    # catmux uses this end of the pair
```

---

## Wine Configuration

Point the COM port at the other end of the tty0tty pair:

```bash
# Set COM4 to tnt1
wine reg add 'HKLM\Software\Wine\Ports' /v COM4 /t REG_SZ /d '/dev/tnt1'

# Verify SERIALCOMM registry key shows COM4
wine reg query 'HKLM\HARDWARE\DEVICEMAP\SERIALCOMM'
```

---

## Signal Mapping

tty0tty crosses signals like a real null-modem cable:

| tnt0 (catmux) sees | When tnt1 (app) asserts |
|--------------------|------------------------|
| CTS | RTS |
| DSR | DTR |
| DCD | DTR |

catmux's signal monitor detects CTS changes on `tnt0` and maps them to the
RTS PTT callback — so your app asserting RTS for PTT works transparently.

---

## Troubleshooting

**`/dev/tnt*` not created after insmod:**
```bash
dmesg | grep tty0tty
lsmod | grep tty0tty
```

**Permission denied on `/dev/tnt0`:**
```bash
sudo chmod a+rw /dev/tnt0 /dev/tnt1
# Or use the udev rule for persistence (see above)
```

**Wine still shows empty COM port dropdown:**
Check that the Windows app uses `MSComm32` or Win32 serial APIs, not a
custom driver. Also verify the `SERIALCOMM` registry key:
```bash
wine reg query 'HKLM\HARDWARE\DEVICEMAP\SERIALCOMM' | grep COM4
```

**App connects but no data:**
Test the tnt pair directly:
```bash
cat /dev/tnt1 &
echo "FA;" > /dev/tnt0
# Should print "FA;" on tnt1
```

Then test through catmux:
```bash
python3 -c "
import serial
s = serial.Serial('/dev/tnt1', 38400, timeout=1)
s.write(b'FA;')
print(repr(s.read(20)))
s.close()
"
```
