# PyMcLauncher (Python Minecraft Launcher)

A fast, lightweight, and modern Minecraft launcher built with Python and PyQt6, supporting both **Windows** and **macOS** (macOS 11.0 Big Sur and newer, covering Apple Silicon M1–M4 and Intel).

---

## Prerequisites

Before running the launcher, make sure you have the following installed on your system:

- **Python 3.10 or newer** (Python 3.11 or 3.12 64-bit recommended).
  - **Windows**: Make sure to check **"Add Python to PATH"** during installation.
  - **macOS**: Install Python 3 via the official [Python.org macOS installer](https://www.python.org/downloads/macos/) or via Homebrew (`brew install python`).
- If you don't know what your doing then download this for:
  - **[Windows](https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe)**
  - **[macOS](https://www.python.org/ftp/python/3.12.7/python-3.12.7-macos11.pkg)**
  - The python page is [here](https://www.python.org/downloads/release/python-3127/)
- **Operating System**:
  - **Windows**: Windows 10 or Windows 11 (64-bit).
  - **macOS**: macOS 11.0 Big Sur, Monterey 12, Ventura 13, Sonoma 14, Sequoia 15+ (Apple Silicon `arm64` and Intel `x86_64`).

---

## Installation

Download the latest version from the [Releases](https://github.com/cloudyhair6/PyMcLauncher/releases/latest) page, or download [`installer.py`](https://github.com/cloudyhair6/PyMcLauncher/releases/latest/download/installer.py) to set up the launcher automatically.

---

## Quick Start (Automatic)

You can automatically check dependencies and launch the application with a single double-click:

### On Windows
Double-click:
```cmd
setup-start.bat
```

### On macOS
Double-click:
```bash
setup-start.command
```

> [!TIP]
> **macOS First-Time Setup**: If macOS displays an execution permission alert when double-clicking for the first time, open Terminal in the launcher folder and run:
> ```bash
> chmod +x setup-start.command
> ```

---

## Manual Setup & Launch

If you prefer to install dependencies and run the launcher from a terminal:

### On Windows (Command Prompt / PowerShell)
```cmd
pip install -r requirements.txt
python launcher.py
```

### On macOS / Linux (Terminal)
```bash
python3 -m pip install -r requirements.txt
python3 launcher.py
```

---

## Key Features

- **Cross-Platform**: Full support for Windows 10/11 and macOS Big Sur 11.0+ (Apple Silicon and Intel).
- **Automated Java Runtime Management**: Automatically detects, downloads, and configures the exact required Eclipse Adoptium Temurin Java runtime (Java 8, 16, 17, 21, or 25) for each Minecraft version.
- **High-Speed Parallel Downloads**: Multi-threaded parallel downloading for assets, libraries, and client jars.
- **Authentication**: Seamless Microsoft Account login (OAuth 2.0 device flow with automatic clipboard copy) and offline player support.
- **Desktop & Start / Applications Shortcuts**: Create one-click launch shortcuts on your Desktop and Start Menu (Windows) or native `.app` application bundles in `~/Applications` (macOS).
- **Source Code Decompiler**: Built-in Minecraft client decompiler powered by CFR with progress tracking.
- **Multi-Instance Profiles**: Manage multiple game directories, version profiles, and custom Java paths independently.

---

## Project Structure

- `launcher.py` — Main graphical launcher interface (PyQt6).
- `setup-start.bat` — One-click launcher script for Windows.
- `setup-start.command` — Double-clickable launcher script for macOS.
- `setup-start.sh` — Terminal launcher script for macOS / Linux.
- `backend/` — Core launcher backend modules:
  - `command_executor.py` — Prepares JVM launch arguments (`-XstartOnFirstThread` for macOS GLFW), classpaths, and runs Minecraft.
  - `download_java.py` — Automated Java runtime downloader (Adoptium Temurin API, bundle path resolution, Gatekeeper quarantine clearance).
  - `download_minecraft.py` — Version manifest parser and client jar downloader.
  - `download_libraries.py` — Parallel library & native binary extractor.
  - `download_assets.py` — Minecraft asset index and sound/texture downloader.
  - `extract_source_code.py` — CFR Java decompiler integration.
  - `ms_accounts.py` — Microsoft and Xbox Live authentication.
  - `utils.py` — Platform utilities, config management, and shortcut creators.
- `images/` — Launcher icons, logos, and UI assets.

---

## Bug Reports & Contributing

If you encounter any issues or bugs, please submit an issue on the [Issues Tracker](https://github.com/cloudyhair6/PyMcLauncher/issues/new).
