# Nuke-SmartFreeze ❄️

A zero-latency performance tool for Foundry Nuke that eliminates UI lag during timeline scrubbing on heavy scripts.

## The Problem
In massive Nuke scripts, scrubbing the timeline can feel incredibly sluggish. This happens because Nuke forces the Node Graph (DAG) to repaint its UI every time the frame changes. If you have thousands of nodes, this UI redraw creates a massive bottleneck, even if your frames are already cached in RAM.

## The Solution
SmartFreeze dynamically swaps your live Node Graph with a cached image (framebuffer) the exact moment you click the timeline. By completely removing the DAG from the active Qt layout during a scrub, Nuke's UI thread is unblocked, resulting in buttery-smooth playback. 

The moment you hover your mouse back over the Node Graph, the live UI is instantly restored.

### Features
* **Zero-Latency:** Uses `QStackedWidget` to swap the UI instead of fighting Qt event propagation.
* **Action-Based Triggers:** Only freezes when you left-click in the Timeline, Dope Sheet, or Curve Editor. You can still pan and interact with the main Viewer image normally.
* **Ghost Widget Protection:** Bulletproof against Nuke's internal C++ garbage collection crashes.
* **Dynamic Group Support:** Automatically detects and handles new tabs when opening Group nodes.

## Installation

1. Download or clone this repository into your `.nuke` folder (or your studio's shared plugin directory).
2. Ensure you have the [Qt.py](https://github.com/mottosso/Qt.py) shim available in your Python environment.
3. Add the following code to your `~/.nuke/menu.py` file to point Nuke to the repo and initialize the tool:

```python
import nuke
import os

# 1. Define the path to the SmartFreeze GitHub folder
smart_freeze_dir = os.path.join(os.path.expanduser("~"), ".nuke", "SmartFreeze")

# 2. Add that folder to Nuke's plugin search path
nuke.pluginAddPath(smart_freeze_dir)

# 3. Import the module to initialize the global Qt event filter
try:
    import SmartFreeze
except Exception as e:
    nuke.warning(f"[SmartFreeze] Failed to load from GitHub repo: {e}")