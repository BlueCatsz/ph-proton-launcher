#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
INSTALL_DIR="$DATA_HOME/ph-proton-launcher"
DESKTOP_DIR="$DATA_HOME/applications"
DESKTOP_FILE="$DESKTOP_DIR/ph-proton-launcher.desktop"
LAUNCHER_BIN="$INSTALL_DIR/ph-proton-launcher"

if ! SYSTEM_PYTHON="$(command -v python3)"; then
    echo "Error: python3 is required but was not found in PATH." >&2
    exit 1
fi

echo "Installing Process Hacker Proton Launcher to $INSTALL_DIR..."

rm -rf "$INSTALL_DIR/ph_proton_launcher"
mkdir -p "$INSTALL_DIR" "$DESKTOP_DIR"

cp "$SCRIPT_DIR/main.py" "$INSTALL_DIR/main.py"
cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"
cp -R "$SCRIPT_DIR/ph_proton_launcher" "$INSTALL_DIR/ph_proton_launcher"
find "$INSTALL_DIR/ph_proton_launcher" -type d -name "__pycache__" -exec rm -rf {} +
chmod 0755 "$INSTALL_DIR/main.py"

if "$SYSTEM_PYTHON" -c "import PyQt6.QtWidgets" >/dev/null 2>&1; then
    PYTHON_BIN="$SYSTEM_PYTHON"
    echo "Using system Python with PyQt6: $PYTHON_BIN"
else
    echo "System PyQt6 not found; creating virtual environment at $INSTALL_DIR/.venv..."
    if ! "$SYSTEM_PYTHON" -m venv "$INSTALL_DIR/.venv"; then
        echo "Error: failed to create virtual environment. Install python3-venv or system PyQt6 (e.g. python3-pyqt6 / python-pyqt6) and retry." >&2
        exit 1
    fi
    "$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip
    "$INSTALL_DIR/.venv/bin/python" -m pip install -r "$INSTALL_DIR/requirements.txt"
    PYTHON_BIN="$INSTALL_DIR/.venv/bin/python"
fi

cat > "$LAUNCHER_BIN" <<EOF
#!/usr/bin/env bash
exec "$PYTHON_BIN" "$INSTALL_DIR/main.py" "\$@"
EOF
chmod 0755 "$LAUNCHER_BIN"

if [[ "$LAUNCHER_BIN" == *[[:space:]]* ]]; then
    DESKTOP_EXEC="\"$LAUNCHER_BIN\""
else
    DESKTOP_EXEC="$LAUNCHER_BIN"
fi

cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Process Hacker (Proton)
GenericName=Proton Process Monitor Launcher
Comment=Launch Process Hacker inside a running Steam Proton prefix
Exec=$DESKTOP_EXEC --auto
Icon=utilities-system-monitor
Terminal=false
Categories=System;Monitor;
Keywords=steam;proton;protontricks;process hacker;wine;debug;
StartupNotify=true
StartupWMClass=ph-proton-launcher
Actions=open-ui;open-verbose;

[Desktop Action open-ui]
Name=Open Launcher UI
Exec=$DESKTOP_EXEC

[Desktop Action open-verbose]
Name=Open with Verbose Logging
Exec=$DESKTOP_EXEC --verbose
EOF
chmod 0644 "$DESKTOP_FILE"

if command -v desktop-file-validate >/dev/null 2>&1; then
    desktop-file-validate "$DESKTOP_FILE"
fi

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
fi

if command -v kbuildsycoca6 >/dev/null 2>&1; then
    kbuildsycoca6 >/dev/null 2>&1 || true
elif command -v kbuildsycoca5 >/dev/null 2>&1; then
    kbuildsycoca5 >/dev/null 2>&1 || true
fi

echo "Installed successfully:"
echo "  Launcher: $LAUNCHER_BIN"
echo "  Desktop entry: $DESKTOP_FILE"
