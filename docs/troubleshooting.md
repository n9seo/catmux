# Troubleshooting

---

## Radio Not Responding

**Symptom:** catmux starts but mirror cache stays empty, timeouts in log.

**Check 1 — Test the port directly** (stop catmux first):

```bash
python3 -c "
import serial
s = serial.Serial('/dev/FT991A_CAT', 38400, timeout=1)
s.rts = True
s.write(b'FA;')
print(repr(s.read(20)))
s.close()
"
```

If this returns empty, the issue is the radio or port, not catmux.

**Check 2 — Baud rate mismatch:**
Verify the baud rate in `catmux.toml` matches the radio's CAT menu setting exactly.

**Check 3 — Wrong USB port:**
The FT-991A presents two USB serial ports. Only `if00` is CAT. Check:
```bash
ls -la /dev/serial/by-id/ | grep -i yaesu
```

**Check 4 — RTS not asserted (Yaesu):**
Make sure `rts_on_connect = true` is set for Yaesu radios.

**Check 5 — Radio in menu:**
Exit all menus on the radio. The FT-991A won't respond to CAT while in menu mode.

---

## Permission Denied on Serial Port

```
PermissionError: [Errno 13] Permission denied: '/dev/FT991A_CAT'
```

Add your user to the serial group:

```bash
# Find the group
stat -c '%G' /dev/FT991A_CAT   # uucp on Arch, dialout on Debian/Ubuntu

# Add user
sudo usermod -aG uucp $USER    # Arch/Manjaro
sudo usermod -aG dialout $USER  # Debian/Ubuntu

# Apply without logging out
newgrp uucp
```

---

## Apps Getting Wrong Responses (Protocol Errors)

**Symptom:** Logger or flrig reports "Protocol Error" or "Get Mode Error".

This is usually caused by response misrouting — one app's response being
delivered to a different app. Common causes:

**1. SH/NA commands flooding the queue:**
Add them to the suppress list for the offending port:
```toml
suppress = ["SH", "NA", "EX"]
```

**2. Two apps polling at the same rate:**
Lower the priority of the less important app:
```toml
priority = 20   # instead of 10
```

**3. Hamlib version mismatch:**
If an app embeds an old hamlib, it may send commands the radio doesn't support.
Check the catmux log for `?;` responses and note which commands they follow.

---

## PTT Delay

**Symptom:** Noticeable delay between keying PTT and radio transmitting.

**Check 1 — PTT method:**
In your app settings, check whether PTT is via RTS, DTR, or CAT. Make sure
`ptt_from` in your vport config matches.

**Check 2 — Timeouts too long:**
Tighten timeouts in `[rig]`:
```toml
response_timeout = 0.2
set_timeout      = 0.05
```

**Check 3 — Queue depth:**
Run with `--status` and watch the queue depth. If it's consistently above 10,
something is flooding the queue. Enable debug logging to find the culprit.

---

## Virtual Ports Not Appearing

**Symptom:** `/home/user/.catmux/vportX` symlinks don't exist.

**Check 1 — symlink_dir exists and is writable:**
```bash
ls -la ~/.catmux/
```

**Check 2 — catmux is running:**
```bash
pgrep -a python | grep catmux
```

PTY devices only exist while catmux holds them open. Symlinks disappear when
catmux stops.

**Check 3 — Check startup logs:**
```bash
journalctl -u catmux.service -n 50
```

---

## Wine App Can't Find COM Port

See `docs/tty0tty.md` for the complete Wine/tty0tty troubleshooting guide.

Quick checks:

```bash
# Verify COM port is in Wine registry
wine reg query 'HKLM\HARDWARE\DEVICEMAP\SERIALCOMM' | grep COM4
wine reg query 'HKLM\Software\Wine\Ports' | grep COM4

# Test the tnt pair
python3 -c "
import serial
s = serial.Serial('/dev/tnt1', 38400, timeout=1)
s.write(b'FA;')
print(repr(s.read(20)))
s.close()
"
```

---

## catmux Crashes / Restarts on Radio Unplug

This is expected — when the USB device disappears the serial port throws an
exception. catmux logs the error and attempts to reconnect.

With udev rules and systemd properly configured, catmux stops cleanly when
the radio is unplugged and restarts automatically when it's replugged.

If the reconnect loop is filling your journal, reduce logging:
```toml
[log]
level = "WARNING"
```

---

## High CPU Usage

**Symptom:** catmux using more CPU than expected.

Usually caused by apps polling too aggressively. Check queue depth with `--status`.

If a specific app is sending thousands of commands per second, add it to the
suppress list or reduce its poll interval in the app's settings.

The mirror cache should absorb most polling — if you see `-> Radio:` lines
for the same command many times per second, the mirror TTL may be too short:

```toml
mirror_ttl = 2.0   # increase from default 1.0
```

---

## Collecting Debug Information

When reporting issues, collect a debug log:

```bash
python3 catmux_main.py --debug 2>&1 | tee /tmp/catmux_debug.log
```

Reproduce the issue, then Ctrl-C. The log file contains every frame sent
and received which makes diagnosis straightforward.
