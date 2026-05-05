#!/usr/bin/env bash
# catmux install script
# Run from the catmux directory: bash install.sh

set -e

CATMUX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER="${SUDO_USER:-$USER}"
HOME_DIR=$(eval echo "~$USER")

echo "=== catmux installer ==="
echo "Directory : $CATMUX_DIR"
echo "User      : $USER"
echo ""

# --- tty0tty ---
if [ -d "$CATMUX_DIR/../tty0tty/module" ]; then
    echo "[1/4] Installing tty0tty kernel module..."
    KERNEL=$(uname -r)
    MODULE_DIR="/lib/modules/$KERNEL/kernel/drivers/tty"
    sudo mkdir -p "$MODULE_DIR"
    sudo cp "$CATMUX_DIR/../tty0tty/module/tty0tty.ko" "$MODULE_DIR/"
    sudo depmod -a
    echo "tty0tty" | sudo tee /etc/modules-load.d/tty0tty.conf > /dev/null
    echo "KERNEL==\"tnt[0-9]*\", MODE=\"0666\"" | sudo tee /etc/udev/rules.d/99-tty0tty.rules > /dev/null
    sudo udevadm control --reload-rules
    echo "  tty0tty installed and will load on boot"
else
    echo "[1/4] tty0tty not found — skipping (build it first if needed)"
    echo "      git clone https://github.com/lcgamboa/tty0tty && cd tty0tty/module && make"
fi

# --- udev rule for catmux symlink dir ---
echo "[2/4] Creating catmux symlink directory..."
CATMUX_SYMLINK_DIR="$HOME_DIR/.catmux"
mkdir -p "$CATMUX_SYMLINK_DIR"
echo "  Created $CATMUX_SYMLINK_DIR"

# --- tty0tty systemd service ---
echo "[3/4] Installing systemd services..."
sudo cp "$CATMUX_DIR/systemd/tty0tty.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable tty0tty.service
echo "  tty0tty.service enabled"

# --- catmux systemd service ---
# Detect python path
PYTHON_BIN=$(which python3)
if command -v pyenv &>/dev/null; then
    PYENV_PYTHON=$(pyenv which python3 2>/dev/null || true)
    [ -n "$PYENV_PYTHON" ] && PYTHON_BIN="$PYENV_PYTHON"
fi

# Generate catmux.service from template with correct paths
sed \
    -e "s|User=n0call|User=$USER|g" \
    -e "s|WorkingDirectory=.*|WorkingDirectory=$CATMUX_DIR|g" \
    -e "s|ExecStart=.*python3|ExecStart=$PYTHON_BIN|g" \
    "$CATMUX_DIR/systemd/catmux.service" | sudo tee /etc/systemd/system/catmux.service > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable catmux.service
echo "  catmux.service enabled"
echo "  Python: $PYTHON_BIN"

# --- serial group ---
echo "[4/4] Checking serial port permissions..."
SERIAL_GROUP=$(stat -c '%G' /dev/ttyUSB0 2>/dev/null || \
               stat -c '%G' /dev/ttyACM0 2>/dev/null || \
               getent group uucp > /dev/null 2>&1 && echo "uucp" || echo "dialout")

if groups "$USER" | grep -qw "$SERIAL_GROUP"; then
    echo "  User '$USER' already in group '$SERIAL_GROUP'"
else
    sudo usermod -aG "$SERIAL_GROUP" "$USER"
    echo "  Added '$USER' to group '$SERIAL_GROUP'"
    echo "  NOTE: Log out and back in for group change to take effect"
fi

echo ""
echo "=== Installation complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit your catmux.toml config if needed"
echo "  2. Start services now:"
echo "       sudo systemctl start tty0tty"
echo "       sudo systemctl start catmux"
echo "  3. Check status:"
echo "       sudo systemctl status catmux"
echo "       journalctl -u catmux -f"
echo ""
echo "On next boot everything starts automatically."
