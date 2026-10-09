# PyMcLauncher (Python Minecraft Launcher)

A custom, lightweight Minecraft launcher built with Python and PyQt6.

## Prerequisites

Before running the launcher, make sure you have the following installed on your system:

- **Python 3.9 or newer** (Make sure to check "Add Python to PATH" during installation)
- **Node.js** (Required for backend protocol scripts)

## Installation & Setup

1. **Install Python Dependencies:**
   Open a terminal in the project folder and run the following command to install the required Python packages:
   ```bash
   pip install -r requirements.txt
   ```

2. **Install Node Modules:**
   node 24.19.0
   Because the `node_modules` folder isn't copied over when sharing the project, you must install the required Node dependencies manually. Run the following command in the project directory:
   ```bash
   npm install
   ```

## Running the Launcher

Once both Python and Node dependencies are successfully installed, you can start the launcher by running:

```bash
python launcher.py
```
