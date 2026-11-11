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

# Install or update required packages
echo "[*] Checking and installing dependencies from requirements.txt..."
if ! "$PYTHON_BIN" -m pip install -r requirements.txt 2>/dev/null; then
    # Fallback for Homebrew PEP 668 externally-managed Python environments
    echo "[*] Retrying with user-scoped packages..."
    "$PYTHON_BIN" -m pip install -r requirements.txt --break-system-packages 2>/dev/null || "$PYTHON_BIN" -m pip install --user -r requirements.txt
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
