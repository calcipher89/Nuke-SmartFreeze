# SmartFreeze for Nuke

A zero-latency performance tool that freezes the Node Graph (DAG) during timeline scrubbing to eliminate UI lag in heavy Nuke scripts.

## Problem

When working with thousands of nodes in Nuke, scrubbing the timeline becomes choppy — even with frames already cached in RAM. Nuke forces the Node Graph to re-render on every frame change, tying up the UI thread and making scrubbing feel sluggish and unresponsive.

## Solution

SmartFreeze captures a framebuffer snapshot of the Node Graph when you start scrubbing the timeline, swaps it into the interface as a static image, and removes the live DAG from the paint path. With the DAG off-screen, Nuke's UI thread is free to update the frame counter smoothly without rendering 60+ times per second. When you release the mouse button (or after a configurable debounce period), the live DAG is restored.

## Features

- **Per-Widget Event Filtering**: Hooks only the target areas (DAG / Viewer / DopeSheet / CurveEditor) with a periodic refresh scan, avoiding the architectural issues that plagued global event filters on Nuke 15.x.

- **Two Trigger Modes**: Freeze from the timeline (click and scrub), from a keyboard shortcut (`ctrl+space` by default), or both. Set it in Nuke Preferences → SmartFreeze → Trigger Mode.
  - *Timeline* is momentary — freezes while you scrub, releases after a short debounce when you let go.
  - *Keyboard* is a sticky toggle — press once to freeze, press again to release. It stays frozen regardless of mouse activity, which is handy for parking the DAG while you work elsewhere.

- **Configurable Unfreeze Delay**: Adjust the timeline-mode debounce delay in Preferences → SmartFreeze → Unfreeze Delay (milliseconds). Default is 20ms for snappy response; tune higher for a gentler settle time. If you click again quickly during rapid scrubbing, the pending unfreeze is canceled and freezing resumes — no flicker.

- **Frozen-State Indicator**: A frozen node graph draws a coloured outline (and optionally a tint wash) over the snapshot, so it's always obvious the DAG is parked rather than broken. Outline colour, width, tint colour and opacity are all adjustable in Preferences. Outline is on by default; tint is off.

- **Recovery Menu**: A top-level **SmartFreeze** menu provides *Toggle Freeze*, *Unfreeze Now* (restores the DAG and sweeps up any stray state), and *Reset SmartFreeze* (full teardown and rebuild) — a one-click escape hatch if anything ever looks stuck.

- **Instant Destruction Detection**: Watches Nuke-owned widgets for destruction signals (workspace swap, panel close, group node close) and recovers cleanly without touching half-destroyed Qt objects.

- **Focus-Loss Safety**: Automatically unfreezes when Nuke loses focus to protect your layout saves from corruption.

- **Zero Setup**: Drop the script in your `.nuke` directory and import from `menu.py`. No configuration needed to get working; everything is optional.

## Installation

1. Clone or download this repository to your `~/.nuke/` folder:

```bash
cd ~/.nuke
git clone https://github.com/calcipher89/Nuke-SmartFreeze.git SmartFreeze
# OR manually copy SmartFreeze.py and LICENSE into a SmartFreeze/ folder
```

2. Add the following line to your `~/.nuke/menu.py`:

```python
nuke.pluginAddPath('./SmartFreeze')
import SmartFreeze
```

3. Restart Nuke. With console logging enabled, you'll see `[SmartFreeze] v4.3 ready.` in the Script Editor output.

4. **(Optional)** Open Nuke Preferences → SmartFreeze to choose the trigger mode, change the shortcut, adjust the unfreeze delay, style the frozen-state indicator, or enable console logging.

**Studio deployment:** default trigger mode, shortcut, and indicator settings are named constants at the top of `SmartFreeze.py` (`DEFAULT_TRIGGER_MODE`, `DEFAULT_SHORTCUT`, `DEFAULT_SHOW_OUTLINE`, `DEFAULT_SHOW_TINT`). They apply the first time an artist's preferences are written; after that, each artist's saved settings win.

## Compatibility

- **Tested**: Nuke 15.1v4 (Linux). Uses PySide2 / shiboken2.
- **Requires**: Qt.py shim (standard in Nuke 14+), Python 3.6+.

## License

MIT License — see [LICENSE](./LICENSE) file for details.

## Troubleshooting

**Q: The timeline still feels laggy.**  
A: SmartFreeze only helps if the DAG is the bottleneck. If your nodes themselves are expensive to update (heavy expressions, deferred plugins), scrubbing will still stall. Use `nuke.Viewer.frameServer()` or render-farm caching for those cases.

**Q: I see console spam.**  
A: Open Nuke Preferences → SmartFreeze → Uncheck "Enable Console Logging". The default is off.

**Q: Freeze isn't triggering on my first click.**  
A: SmartFreeze installs event filters on a 5-second refresh cycle. Click on the timeline, wait up to 5 seconds, click again. On a fresh Nuke launch it should be immediate, but if you open new panels (e.g., split a Viewer) there's a short lag before the hook reaches them. This is by design — frequent widget scanning is expensive.

**Q: The node graph looks stuck frozen.**  
A: First check whether you started a keyboard freeze — that one is sticky and stays until you press the shortcut again. If it's genuinely stuck, use **SmartFreeze → Unfreeze Now**. If that doesn't clear it, **SmartFreeze → Reset SmartFreeze** rebuilds the tool from scratch.

**Q: The keyboard shortcut doesn't do anything.**  
A: Check that Trigger Mode is set to *Keyboard* or *Both*. On Linux, input-method frameworks (ibus, fcitx) often grab `ctrl+space` for language switching; either change that binding at the desktop level or set a different shortcut in Preferences. Shortcut changes take effect after restarting Nuke.

**Q: Double-clicking the timeline turns on the in/out points.**  
A: That's standard Nuke behaviour and happens with SmartFreeze disabled too. Clear them with `alt+u`.

## Version History

- **v4.3**: Keyboard trigger (sticky toggle, `ctrl+space` default) with Timeline / Keyboard / Both trigger modes; frozen-state indicator with configurable outline and tint.
- **v4.2**: Fixed a rapid-click race that could leave the node graph stuck frozen; added the SmartFreeze recovery menu.
- **v4.1**: Per-widget event filters (workspace-swap crash fix), release-debounce trigger, configurable unfreeze delay, ASCII-only logging, MIT license.
- **v3.9**: Original architecture with QApplication-wide event filter.

## Credits

SmartFreeze was born from a need to make timeline scrubbing responsive in studios running heavy Nuke scripts on high-core-count machines. It's a simple tool that solves a real problem — use it and let me know if it helps.
