# systemd Service Setup

catmux can run as a systemd service so it starts automatically when your
radio is plugged in and stops when it's unplugged.

---

## Prerequisites

- catmux installed and working from the command line
- udev rules configured (see `docs/udev.md`)
- tty0tty installed if using Wine/Windows apps (see `docs/tty0tty.md`)

---

## Service File

Copy the included service file and edit it for your system:

```bash
sudo cp systemd/catmux.service /etc/systemd/system/
sudo nano /etc/systemd/system/catmux.service
```

Key fields to edit:

```ini
[Service]
User=yourusername
WorkingDirectory=/home/yourusername/code/catmux
ExecStart=/home/yourusername/.pyenv/versions/catmux/bin/python3 catmux_main.py --config catmux.toml
```

Set `User` to your username, `WorkingDirectory` to where catmux lives, and
`ExecStart` to the full path of your Python interpreter (use `which python3`
or `pyenv which python3` to find it).

---

## Full Service File

```ini
[Unit]
Description=catmux CAT serial multiplexer
After=tty0tty.service
Wants=tty0tty.service
StopWhenUnneeded=yes

[Service]
Type=simple
User=yourusername
WorkingDirectory=/home/yourusername/code/catmux
ExecStart=/home/yourusername/.pyenv/versions/catmux/bin/python3 catmux_main.py --config catmux.toml
Restart=no
StandardOutput=journal
StandardError=journal
SyslogIdentifier=catmux

[Install]
WantedBy=multi-user.target
```

`StopWhenUnneeded=yes` combined with the udev `SYSTEMD_WANTS` tag means:
- catmux starts automatically when the radio is plugged in
- catmux stops automatically when the radio is unplugged

`Restart=no` — udev manages start/stop, not systemd auto-restart.

---

## Enable and Start

```bash
sudo systemctl daemon-reload
sudo systemctl enable catmux.service
sudo systemctl start catmux.service
```

Check status:

```bash
sudo systemctl status catmux.service
```

View live logs:

```bash
journalctl -u catmux.service -f
```

---

## tty0tty Service

If you use the `device` option for Wine apps, tty0tty needs to load before
catmux. The included `tty0tty.service` handles this:

```bash
sudo cp systemd/tty0tty.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable tty0tty.service
sudo systemctl start tty0tty.service
```

The catmux service already has `After=tty0tty.service` so ordering is correct.

---

## Checking Logs

```bash
# Live log
journalctl -u catmux.service -f

# Last 100 lines
journalctl -u catmux.service -n 100

# Since last boot
journalctl -u catmux.service -b

# With debug output (if catmux was started with --debug)
journalctl -u catmux.service -f | grep -E "Radio|Mirror|PTT"
```

---

## Troubleshooting

**Service fails to start:**
```bash
journalctl -u catmux.service -n 50
```
Look for permission errors (wrong serial group) or port not found (udev rule not matched).

**Service starts but radio not responding:**
```bash
# Test the port directly
python3 -c "
import serial
s = serial.Serial('/dev/FT991A_CAT', 38400, timeout=1)
s.rts = True
s.write(b'FA;')
print(repr(s.read(20)))
s.close()
"
```

**Service doesn't start on plug-in:**
```bash
# Check udev is firing
sudo udevadm monitor --udev | grep -E "FT991|ttyUSB"
# Then plug in the radio
```

**Service doesn't stop on unplug:**
Check that `StopWhenUnneeded=yes` is in the `[Service]` section and that the
udev rule has `TAG+="systemd"` and `ENV{SYSTEMD_WANTS}="catmux.service"`.
