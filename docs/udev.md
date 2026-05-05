# udev Rules for catmux

Using udev rules to create stable device names is strongly recommended over
relying on `/dev/ttyUSBX` which can change depending on plug-in order.

---

## Finding Your Device Attributes

Plug in your radio and run:

```bash
udevadm info -a /dev/ttyUSB0 | grep -E "idVendor|idProduct|serial"
```

Note the `idVendor`, `idProduct`, and `serial` values — these uniquely identify
your specific radio and won't change.

For a CP2105 dual UART (used in the FT-991A):

```
ATTRS{idVendor}=="10c4"
ATTRS{idProduct}=="ea70"
ATTRS{serial}=="01CF0CAE"
```

---

## Example Rules File

Create `/etc/udev/rules.d/99-myradio.rules`:

### Yaesu FT-991A (CP2105 dual UART)

```
# Interface 0 = CAT control
SUBSYSTEM=="tty", \
  ATTRS{idVendor}=="10c4", \
  ATTRS{idProduct}=="ea70", \
  ATTRS{serial}=="01CF0CAE", \
  ENV{ID_USB_INTERFACE_NUM}=="00", \
  SYMLINK+="FT991A_CAT", \
  TAG+="systemd", \
  ENV{SYSTEMD_WANTS}="catmux.service"

# Interface 1 = audio/data (not used for CAT)
SUBSYSTEM=="tty", \
  ATTRS{idVendor}=="10c4", \
  ATTRS{idProduct}=="ea70", \
  ATTRS{serial}=="01CF0CAE", \
  ENV{ID_USB_INTERFACE_NUM}=="01", \
  SYMLINK+="FT991A_DATA"
```

### Elecraft KX3 (FTDI)

```
SUBSYSTEM=="tty", \
  ATTRS{idVendor}=="0403", \
  ATTRS{idProduct}=="6001", \
  ATTRS{serial}=="YOURSERIAL", \
  SYMLINK+="KX3_CAT", \
  TAG+="systemd", \
  ENV{SYSTEMD_WANTS}="catmux.service"
```

### Icom IC-7100 (Silicon Labs)

```
SUBSYSTEM=="tty", \
  ATTRS{idVendor}=="10c4", \
  ATTRS{idProduct}=="ea60", \
  ATTRS{serial}=="YOURSERIAL", \
  SYMLINK+="IC7100_CAT", \
  TAG+="systemd", \
  ENV{SYSTEMD_WANTS}="catmux.service"
```

---

## Systemd Integration

The `TAG+="systemd"` and `ENV{SYSTEMD_WANTS}="catmux.service"` lines tell
systemd to automatically start catmux when the radio is plugged in, and stop
it when unplugged (when combined with `StopWhenUnneeded=yes` in the service).

See `docs/systemd.md` for the full service configuration.

---

## Applying Rules

After creating or editing the rules file:

```bash
sudo udevadm control --reload-rules
sudo udevadm trigger
```

Verify the symlink was created:

```bash
ls -la /dev/FT991A_CAT
```

---

## Determining Interface Numbers

For radios with multiple USB interfaces (like the FT-991A's dual UART), you
need to identify which interface number is the CAT port:

```bash
udevadm info -a /dev/ttyUSB0 | grep "interface"
udevadm info -a /dev/ttyUSB1 | grep "interface"
```

Or watch which device appears first when you plug in:

```bash
sudo udevadm monitor --udev | grep tty
```

Then plug in the radio and observe which `ttyUSBX` appears for which interface.
