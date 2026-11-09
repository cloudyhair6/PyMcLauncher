# PyMcLauncher (Python Minecraft Launcher)

A custom, fast, lightweight Minecraft launcher built with Python and PyQt6.

## Prerequisites

Before running the launcher, make sure you have the following installed on your system:

- **Python 3.10 or newer** (Python 3.11 or 3.12 64-bit recommended for best performance).
- **Important**: Make sure to check **"Add Python to PATH"** during Python installation.

---

Follow the intructions on the latest [release](https://github.com/cloudyhair6/PyMcLauncher/releases/latest) or just download the latest [installer.py](https://github.com/cloudyhair6/PyMcLauncher/releases/latest/download/installer.py)

## Quick Start (Automatic)

You can automatically check for updates, install required dependencies, and launch the application simply by double-clicking:

```bat
setup-start.bat
```

This batch script automatically handles dependency checks and starts the launcher for you.

# BUT download the [installer.py](https://github.com/cloudyhair6/PyMcLauncher/releases/latest/download/installer.py) first.

---

## Manual Setup & Launch

If you prefer to install dependencies and run the launcher manually from a terminal:

1. **Install Dependencies:**
   Open a terminal (Command Prompt or PowerShell) in the project directory and run:
   ```bash
   pip install -r requirements.txt
   ```

2. **Run the Launcher:**
   ```bash
   python launcher.py
   ```

---

any bugs please report in the [issues](https://github.com/cloudyhair6/PyMcLauncher/issues/new).

## Project Structure

- `launcher.py` — Main graphical launcher interface (PyQt6).
- `backend/` — Core launcher backend modules (downloaders, launch executors, Java runtime management, utilities).
- `images/` — Launcher graphical assets, logos, and locator bar textures.
- `.cache/` — User authentication cache, session tokens, and Python bytecode caches.
- `launcher_config.json` — User preferences, profile configurations, and custom directories.
