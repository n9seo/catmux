#!/usr/bin/env bash
# catmux - serial port permission setup
# Run once after install: bash setup_permissions.sh

set -e

# Detect which group owns the serial devices on this system
detect_serial_group() {
    local port
    # Try common USB serial devices first, fall back to ttyS0
    for dev in /dev/ttyUSB0 /dev/ttyUSB1 /dev/ttyACM0 /dev/ttyS0; do
        if [ -e "$dev" ]; then
            stat -c '%G' "$dev"
            return
        fi
    done
    # Nothing plugged in yet — check which group exists
    if getent group dialout > /dev/null 2>&1; then
        echo "dialout"
    elif getent group uucp > /dev/null 2>&1; then
        echo "uucp"
    else
        echo ""
    fi
}

SERIAL_GROUP=$(detect_serial_group)

if [ -z "$SERIAL_GROUP" ]; then
    echo "ERROR: Could not detect serial port group."
    echo "       Plug in your radio's USB cable and re-run this script."
    exit 1
fi

echo "Detected serial group: $SERIAL_GROUP"

if groups "$USER" | grep -qw "$SERIAL_GROUP"; then
    echo "User '$USER' is already in group '$SERIAL_GROUP' — nothing to do."
else
    echo "Adding '$USER' to group '$SERIAL_GROUP'..."
    sudo usermod -aG "$SERIAL_GROUP" "$USER"
    echo ""
    echo "Done. You MUST log out and back in (or run 'newgrp $SERIAL_GROUP')"
    echo "for the group change to take effect."
fi

echo ""
echo "To verify after re-login:"
echo "  groups | grep $SERIAL_GROUP"
