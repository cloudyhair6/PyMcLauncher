#!/bin/bash

# Ensure script runs from its directory regardless of where it was launched
cd "$(dirname "$0")"

echo "=========================================="
echo "         PyMcLauncher for macOS           "
echo "=========================================="

# Check for Python 3 installation
if command -v python3 &>/dev/null; then
    PYTHON_BIN="python3"
elif command -v python &>/dev/null && python -c "import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)" &>/dev/null; then
    PYTHON_BIN="python"
else
    echo ""
    echo "[Error] Python 3 was not found on your system."
    echo "Please install Python 3 from https://www.python.org/downloads/macos/ or via Homebrew (brew install python)."
    echo ""
    read -n 1 -s -r -p "Press any key to exit..."
    echo ""
    exit 1
fi

echo "[*] Using Python: $($PYTHON_BIN --version 2>&1)"

# Automatically run Install Certificates.command on macOS if present
PY_VER=$("$PYTHON_BIN" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
CERT_CMD="/Applications/Python ${PY_VER}/Install Certificates.command"
if [ -f "$CERT_CMD" ]; then
    echo "[*] Checking macOS SSL certificates..."
    /bin/sh "$CERT_CMD" 2>/dev/null || true
fi

# Upgrade pip to ensure support for modern pre-compiled wheel tags
echo "[*] Ensuring pip is up to date..."
"$PYTHON_BIN" -m pip install --upgrade pip --quiet 2>/dev/null || true

# Install or update required packages (preferring binary wheels to avoid compiling from source)
echo "[*] Installing dependencies (preferring pre-compiled binary wheels)..."
INSTALL_OK=0

if "$PYTHON_BIN" -m pip install --prefer-binary -r requirements.txt; then
    INSTALL_OK=1
elif "$PYTHON_BIN" -m pip install --prefer-binary -r requirements.txt --break-system-packages; then
    INSTALL_OK=1
elif "$PYTHON_BIN" -m pip install --prefer-binary --user -r requirements.txt; then
    INSTALL_OK=1
fi

if [ $INSTALL_OK -ne 1 ]; then
    echo "[*] Applying targeted package fallback for macOS Big Sur..."
    # 1. Install pure Python core dependencies first
    "$PYTHON_BIN" -m pip install requests minecraft-launcher-lib nbt nbtlib certifi msal --quiet 2>/dev/null || \
    "$PYTHON_BIN" -m pip install requests minecraft-launcher-lib nbt nbtlib certifi msal --break-system-packages --quiet 2>/dev/null || true

    # 2. Specifically install macOS Big Sur compatible PyQt6 binary wheel (<6.8.0)
    "$PYTHON_BIN" -m pip install --prefer-binary "PyQt6<6.8.0" "PyQt6-WebEngine<6.8.0" 2>/dev/null || \
    "$PYTHON_BIN" -m pip install --prefer-binary "PyQt6<6.8.0" "PyQt6-WebEngine<6.8.0" --break-system-packages 2>/dev/null || true
fi

# Verify critical dependencies before launching
if ! "$PYTHON_BIN" -c "import requests" 2>/dev/null; then
    echo ""
    echo "[!] Critical module 'requests' is not installed."
    echo "Please check your network connection and run: $PYTHON_BIN -m pip install -r requirements.txt"
    echo ""
    read -n 1 -s -r -p "Press any key to exit..."
    echo ""
    exit 1
fi

# Launch the launcher
echo "[*] Starting PyMcLauncher..."
"$PYTHON_BIN" launcher.py "$@"

EXIT_STATUS=$?
if [ $EXIT_STATUS -ne 0 ]; then
    echo ""
    echo "[!] Launcher exited with error code $EXIT_STATUS."
    echo "The script will exit after you continue."
    read -n 1 -s -r -p "Press any key to exit..."
    echo ""
fi
