"""
SmartFreeze for Nuke (v4.3)
===========================

A performance tool that freezes the Node Graph (DAG) during timeline
scrubbing to eliminate UI lag in heavy Nuke scripts.

How it works
------------
When the user left-clicks the timeline (or DopeSheet / CurveEditor),
SmartFreeze grabs an OpenGL framebuffer snapshot of each visible DAG
widget, swaps it into the surrounding QStackedWidget as a static image,
and removes the live DAG from the active Qt layout. With the DAG off
the paint path, Nuke's UI thread is no longer forced to re-render the
node graph on every frame change -- timeline scrubbing becomes smooth.

When the user releases the mouse button, a configurable debounce timer
(default 20ms) starts; if no new click arrives before it elapses, the
live DAG is restored. If the user clicks again before the debounce
fires (rapid scrubbing), the pending unfreeze is canceled.

Architecture
------------
Event filtering is **per-widget**, not application-wide. A 5-second
refresh timer scans visible widgets, identifies the freeze trigger
areas by object name (DAG / Viewer / DopeSheet / CurveEditor), and
installs the event filter on those widgets and their descendants. A
property tag prevents double-hooking. This avoids the workspace-swap
SIGSEGV that a QApplication-level event filter is prone to in Nuke
15.x, because we never enter Python's event-dispatch path for the
thousands of teardown events fired on dying child widgets.

Liveness of tracked widgets is checked via shiboken2.isValid() because
Foundry's PySide2 build for Nuke 15.x does not expose QtCore.QPointer.
destroyed() signals are connected on every tracked widget at freeze
time, so workspace swaps trigger an instant state purge without any
widget access from inside Qt's destruction sequence.

A 250ms slow-poll provides a secondary safety net for focus-loss
unfreeze (protects Nuke's layout saves when the app is backgrounded)
and for zombie state cleanup in case destroyed() doesn't fire.

Orphaned preview recovery (v4.2)
--------------------------------
deleteLater() does not destroy immediately -- it posts a DeferredDelete
event. Under rapid click/release/click, the previous cycle's preview
could be destroyed *after* the next freeze had already begun, and its
destroyed() signal would then wipe the tracking state for the NEW
preview. That preview stayed in Nuke's QStackedWidget with nothing
referencing it: the node graph showed a stale snapshot permanently,
and because _freeze() skipped any stack already showing a preview, it
could not recover on its own.

v4.2 addresses this three ways:
  * The preview's own destroyed() signal is no longer connected. We
    own that widget's entire lifecycle, so the signal told us nothing
    we did not already know, and it was the direct cause of the race.
    stack and restore-widget signals are still watched, since those
    belong to Nuke and can genuinely vanish underneath us.
  * The destroyed() slot now defers to the next event-loop turn and
    re-checks which tracked widgets are actually dead, instead of
    purging blindly. A late signal from a previous cycle finds all
    current tracking valid and is ignored. Panes that are still alive
    get properly restored rather than abandoned.
  * _freeze() now sweeps orphaned previews out of a stack and restores
    the real widget underneath, rather than skipping the stack. The
    same sweep is available on demand from the SmartFreeze menu.

Triggers (v4.3)
---------------
Trigger Mode in Preferences selects how a freeze starts:
  * Timeline  -- left-click and scrub the timeline (momentary; releases
                 after the debounce once the mouse comes up).
  * Keyboard  -- a shortcut, ctrl+space by default, toggles a sticky
                 freeze that persists until pressed again. Sticky
                 freezes ignore the mouse-release debounce entirely.
  * Both      -- either works.

A frozen node graph draws an indicator over the snapshot: an outline by
default, with an optional colour wash. Colour, width and opacity are all
exposed in Preferences. The outline is on and the tint is off out of the
box -- a heavy wash over the whole panel tends to read as a broken UI to
someone who did not install the tool themselves.

License: MIT -- see LICENSE.
"""

import nuke
from Qt import QtCore, QtWidgets, QtGui

# Foundry's PySide2 for Nuke 15.x does not expose QtCore.QPointer, so
# we use shiboken's isValid() for liveness checks instead. Falls back
# to shiboken6 for future Nuke versions on PySide6.
try:
    from shiboken2 import isValid as _shiboken_is_valid
except ImportError:
    try:
        from shiboken6 import isValid as _shiboken_is_valid
    except ImportError:
        def _shiboken_is_valid(obj):
            return obj is not None


def is_valid(obj):
    """True iff `obj` is non-None and its underlying C++ QObject still
    exists. Never touches Qt API on `obj`; safe to call mid-teardown."""
    if obj is None:
        return False
    try:
        return _shiboken_is_valid(obj)
    except Exception:
        return False


_PREFS = nuke.toNode("preferences")


# --- DEPLOYMENT DEFAULTS ---
# Change these to set studio-wide defaults. They only apply the first time
# a user's preferences.nk is written; after that each artist's own saved
# value wins.
DEFAULT_TRIGGER_MODE = "Both"        # Timeline | Keyboard | Both
DEFAULT_SHORTCUT = "ctrl+space"
DEFAULT_SHOW_OUTLINE = True
DEFAULT_SHOW_TINT = False

TRIGGER_MODES = ["Timeline", "Keyboard", "Both"]


# --- CACHED PREFERENCES ---
# Read once at startup and refreshed via the addKnobChanged callback
# below. The event filter and slow poll read these directly without
# touching the Nuke knob system on the hot path.
_PREF_ENABLED_CACHE = True
_PREF_LOGGING_CACHE = False
_PREF_DELAY_CACHE = 20  # Default 20ms unfreeze debounce
_PREF_TRIGGER_MODE_CACHE = DEFAULT_TRIGGER_MODE
_PREF_SHOW_OUTLINE_CACHE = DEFAULT_SHOW_OUTLINE
_PREF_OUTLINE_COLOR_CACHE = (0.90, 0.62, 0.20)   # muted amber
_PREF_OUTLINE_WIDTH_CACHE = 2
_PREF_SHOW_TINT_CACHE = DEFAULT_SHOW_TINT
_PREF_TINT_COLOR_CACHE = (0.10, 0.10, 0.10)      # near-black wash
_PREF_TINT_OPACITY_CACHE = 0.25

# The knobs whose changes should refresh the cache.
_WATCHED_KNOBS = (
    "smartfreeze_enable",
    "smartfreeze_logging",
    "smartfreeze_delay",
    "smartfreeze_trigger_mode",
    "smartfreeze_show_outline",
    "smartfreeze_outline_color",
    "smartfreeze_outline_width",
    "smartfreeze_show_tint",
    "smartfreeze_tint_color",
    "smartfreeze_tint_opacity",
)


def _read_color_knob(knob, fallback):
    """Pull an (r, g, b) float triple out of a Nuke Color_Knob.

    Array_Knob exposes per-channel access as value(index), but the exact
    API has shifted between Nuke versions, so fall back to getValue()
    before giving up on the default."""
    if knob is None:
        return fallback
    try:
        return (float(knob.value(0)), float(knob.value(1)), float(knob.value(2)))
    except Exception:
        pass
    try:
        vals = knob.getValue()
        if isinstance(vals, (list, tuple)) and len(vals) >= 3:
            return (float(vals[0]), float(vals[1]), float(vals[2]))
    except Exception:
        pass
    return fallback


def _update_pref_caches():
    """Refresh the module-level pref cache from the actual knob values."""
    global _PREF_ENABLED_CACHE, _PREF_LOGGING_CACHE, _PREF_DELAY_CACHE
    global _PREF_TRIGGER_MODE_CACHE
    global _PREF_SHOW_OUTLINE_CACHE, _PREF_OUTLINE_COLOR_CACHE
    global _PREF_OUTLINE_WIDTH_CACHE
    global _PREF_SHOW_TINT_CACHE, _PREF_TINT_COLOR_CACHE
    global _PREF_TINT_OPACITY_CACHE
    try:
        en_knob = _PREFS.knob("smartfreeze_enable")
        _PREF_ENABLED_CACHE = en_knob.value() if en_knob else True

        log_knob = _PREFS.knob("smartfreeze_logging")
        _PREF_LOGGING_CACHE = log_knob.value() if log_knob else False

        delay_knob = _PREFS.knob("smartfreeze_delay")
        if delay_knob:
            _PREF_DELAY_CACHE = int(delay_knob.value())

        mode_knob = _PREFS.knob("smartfreeze_trigger_mode")
        if mode_knob:
            mode = mode_knob.value()
            if mode in TRIGGER_MODES:
                _PREF_TRIGGER_MODE_CACHE = mode

        outline_knob = _PREFS.knob("smartfreeze_show_outline")
        if outline_knob:
            _PREF_SHOW_OUTLINE_CACHE = bool(outline_knob.value())

        _PREF_OUTLINE_COLOR_CACHE = _read_color_knob(
            _PREFS.knob("smartfreeze_outline_color"), _PREF_OUTLINE_COLOR_CACHE)

        width_knob = _PREFS.knob("smartfreeze_outline_width")
        if width_knob:
            _PREF_OUTLINE_WIDTH_CACHE = max(0, min(10, int(width_knob.value())))

        tint_knob = _PREFS.knob("smartfreeze_show_tint")
        if tint_knob:
            _PREF_SHOW_TINT_CACHE = bool(tint_knob.value())

        _PREF_TINT_COLOR_CACHE = _read_color_knob(
            _PREFS.knob("smartfreeze_tint_color"), _PREF_TINT_COLOR_CACHE)

        opacity_knob = _PREFS.knob("smartfreeze_tint_opacity")
        if opacity_knob:
            _PREF_TINT_OPACITY_CACHE = max(0.0, min(1.0, float(opacity_knob.value())))
    except Exception:
        pass


def _on_pref_changed():
    """Fires when any knob on the Preferences node changes. Filters for
    our knobs and refreshes the cache. Runs on the Nuke main thread when
    the user clicks a checkbox -- never during workspace teardown."""
    try:
        node = nuke.thisNode()
        knob = nuke.thisKnob()
        if node is not _PREFS:
            return
        if knob is None or knob.name() not in _WATCHED_KNOBS:
            return
    except Exception:
        return
    _update_pref_caches()

    # If a preview is on screen right now, repaint it so appearance edits
    # are visible immediately rather than at the next freeze.
    inst = getattr(nuke, '_viewer_smart_freeze', None)
    if inst is not None:
        try:
            inst.refresh_previews()
        except Exception:
            pass


def log(msg):
    if _PREF_LOGGING_CACHE:
        print(msg)


class DummyPreview(QtWidgets.QLabel):
    def __init__(self, pixmap, parent=None):
        super().__init__(parent)
        self.setPixmap(pixmap)
        self.setScaledContents(True)
        # A QLabel takes no focus by default; state it anyway so the
        # preview can never pull focus away from the panel the artist is
        # actually working in.
        self.setFocusPolicy(QtCore.Qt.NoFocus)

    def paintEvent(self, event):
        """Draw the snapshot, then the frozen-state indicator on top.

        Everything is drawn in widget coordinates, not pixmap
        coordinates: setScaledContents stretches the pixmap to fit, so
        pixmap coords would drift out of alignment on panel resize."""
        super(DummyPreview, self).paintEvent(event)

        if not (_PREF_SHOW_TINT_CACHE or _PREF_SHOW_OUTLINE_CACHE):
            return

        painter = QtGui.QPainter(self)
        try:
            rect = self.rect()

            if _PREF_SHOW_TINT_CACHE:
                r, g, b = _PREF_TINT_COLOR_CACHE
                tint = QtGui.QColor(
                    int(max(0.0, min(1.0, r)) * 255),
                    int(max(0.0, min(1.0, g)) * 255),
                    int(max(0.0, min(1.0, b)) * 255),
                )
                tint.setAlphaF(_PREF_TINT_OPACITY_CACHE)
                painter.fillRect(rect, tint)

            width = _PREF_OUTLINE_WIDTH_CACHE
            if _PREF_SHOW_OUTLINE_CACHE and width > 0:
                r, g, b = _PREF_OUTLINE_COLOR_CACHE
                pen = QtGui.QPen(QtGui.QColor(
                    int(max(0.0, min(1.0, r)) * 255),
                    int(max(0.0, min(1.0, g)) * 255),
                    int(max(0.0, min(1.0, b)) * 255),
                ))
                pen.setWidth(width)
                painter.setPen(pen)
                painter.setBrush(QtCore.Qt.NoBrush)
                # Inset by half the pen width so the whole stroke lands
                # inside the widget instead of being clipped in half.
                inset = int(width / 2)
                painter.drawRect(rect.adjusted(inset, inset, -inset - 1, -inset - 1))
        finally:
            painter.end()


class ViewerSmartFreeze(QtCore.QObject):
    # Interval between hook refreshes. The original v3.x build used 1000ms
    # which scanned QApplication.allWidgets() once per second; 5000ms is
    # easily fast enough to pick up newly-opened Group nodes or panel
    # splits, and reduces baseline CPU cost ~5x.
    HOOK_REFRESH_INTERVAL_MS = 5000

    # Slow-poll interval: catches focus loss and zombie state as a
    # belt-and-braces backup to the destroyed() signal connections.
    SAFETY_POLL_INTERVAL_MS = 250

    # Object name fragments identifying widgets we want to filter events on.
    TARGET_NAME_FRAGMENTS = ('DAG', 'Viewer', 'DopeSheet', 'CurveEditor')

    # Property tag stored on each hooked widget so we can short-circuit
    # re-hooking on every refresh cycle.
    HOOK_PROPERTY = "SmartFreezeHooked"

    # Property tag marking a widget whose destroyed() signal we have
    # already connected. Without this, every freeze would add another
    # connection to the same long-lived stack, accumulating thousands
    # of connections over a working day.
    DESTROY_WATCH_PROPERTY = "SmartFreezeDestroyWatch"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._frozen = False
        self._is_unfreezing = False

        # True when the freeze was started by the keyboard toggle. A
        # sticky freeze ignores the mouse-release debounce entirely and
        # persists until toggled off again.
        self._sticky = False

        # Each entry: {'stack': widget, 'dummy': widget, 'restore': widget}
        self._current_dummies = []
        self._dag_rects = []

        # Debounce timer for unfreeze on mouse-release. Interval is set
        # per-fire from the user-configurable pref cache.
        self._unfreeze_timer = QtCore.QTimer(self)
        self._unfreeze_timer.setSingleShot(True)
        self._unfreeze_timer.timeout.connect(self._do_unfreeze)

        # Slow background safety poll (focus loss + zombie state).
        self._slow_timer = QtCore.QTimer(self)
        self._slow_timer.setInterval(self.SAFETY_POLL_INTERVAL_MS)
        self._slow_timer.timeout.connect(self._slow_poll)
        self._slow_timer.start()

        # Hook-refresh timer.
        self._hook_timer = QtCore.QTimer(self)
        self._hook_timer.setInterval(self.HOOK_REFRESH_INTERVAL_MS)
        self._hook_timer.timeout.connect(self._refresh_localized_hooks)
        self._hook_timer.start()

        # Bootstrap scan: hook anything that already exists so the first
        # timeline click works immediately instead of waiting up to 5s
        # for the periodic refresh.
        self._refresh_localized_hooks()

        app = QtWidgets.QApplication.instance()
        app.aboutToQuit.connect(self.cleanup)

        log("[SmartFreeze] v4.3 ready.")

    # ---- HOOK MANAGEMENT --------------------------------------------------

    def _apply_hook(self, widget):
        """Install the event filter on `widget` if it isn't already hooked.
        Uses a Qt property tag to track which widgets we've hooked, so
        repeated calls short-circuit quickly."""
        try:
            if not widget.property(self.HOOK_PROPERTY):
                widget.installEventFilter(self)
                widget.setProperty(self.HOOK_PROPERTY, True)
        except RuntimeError:
            pass

    def _refresh_localized_hooks(self):
        """Walk visible widgets, install filters on target areas and their
        descendants. _apply_hook short-circuits on already-hooked widgets
        via the property tag, so the steady-state cost is just iteration."""
        if not _PREF_ENABLED_CACHE:
            return

        for w in QtWidgets.QApplication.allWidgets():
            try:
                name = w.objectName()
            except RuntimeError:
                continue

            if not any(x in name for x in self.TARGET_NAME_FRAGMENTS):
                continue

            self._apply_hook(w)

            # Mouse events are delivered to the leaf widget under the
            # cursor, not bubbled to ancestors, so we have to hook the
            # children too. findChildren is recursive by default.
            try:
                for child in w.findChildren(QtWidgets.QWidget):
                    self._apply_hook(child)
            except RuntimeError:
                continue

    # ---- HELPERS ----------------------------------------------------------

    def _find_stack(self, widget):
        if widget is None:
            return None
        try:
            w = widget.parent()
        except RuntimeError:
            return None
        while w:
            if isinstance(w, QtWidgets.QStackedWidget):
                return w
            try:
                w = w.parent()
            except RuntimeError:
                return None
        return None

    def _get_dag_gl_widgets(self):
        gl_widgets = []
        for w in QtWidgets.QApplication.allWidgets():
            try:
                name = w.objectName()
            except RuntimeError:
                continue
            if not name.startswith('DAG'):
                continue
            if not isinstance(w, QtWidgets.QWidget):
                continue
            try:
                if not w.isVisible():
                    continue
            except RuntimeError:
                continue
            if hasattr(w, 'grabFrameBuffer') or hasattr(w, 'grabFramebuffer'):
                gl_widgets.append(w)
        return gl_widgets

    def _is_target_area(self, widget, global_pos):
        """Is the widget under the cursor a freeze trigger area? Triggers
        on the DopeSheet, the CurveEditor, or the Viewer's bottom strip
        (the timeline scrubber)."""
        if widget is None:
            return False
        w = widget
        viewer_widget = None
        while w:
            try:
                name = w.objectName()
                if 'DopeSheet' in name or 'CurveEditor' in name:
                    return True
                if 'Viewer' in name:
                    viewer_widget = w
                    break
                w = w.parent()
            except RuntimeError:
                return False

        if viewer_widget:
            try:
                if widget.height() < 80:
                    local_pos = viewer_widget.mapFromGlobal(global_pos)
                    if local_pos.y() > (viewer_widget.height() * 0.75):
                        return True
            except RuntimeError:
                return False
        return False

    def _check_layout_integrity(self):
        """True if all tracked widgets are still live C++ objects."""
        if not self._frozen:
            return True
        if not self._current_dummies:
            return False
        for item in self._current_dummies:
            if not is_valid(item.get('stack')) or not is_valid(item.get('dummy')):
                return False
        return True

    def _purge_state(self):
        """Wipe state without touching any tracked widget. Safe to call
        from a destroyed() slot or after partial C++ teardown."""
        self._current_dummies = []
        self._dag_rects = []
        self._frozen = False
        self._is_unfreezing = False
        self._sticky = False
        try:
            self._unfreeze_timer.stop()
        except Exception:
            pass

    # ---- DESTROYED SIGNAL SLOT -------------------------------------------

    def _on_tracked_destroyed(self, *_):
        """Fires from inside Qt's destruction sequence for a watched widget
        (workspace swap, panel close, group close).

        MUST NOT touch any widget -- the C++ side may be partially gone.
        All we do here is schedule a check for the next event-loop turn,
        once destruction has settled."""
        if not self._frozen:
            return
        QtCore.QTimer.singleShot(0, self._deferred_recover)

    def _deferred_recover(self):
        """Runs one event-loop turn after a watched widget was destroyed.

        Re-checks whether anything we *currently* track actually died. A
        signal left over from a previous freeze cycle will find all
        current tracking valid and is ignored -- this is what stops a
        late destruction from wiping the state of a freeze that started
        after it. If something genuinely died, _do_unfreeze restores
        every pane that is still alive and drops only the dead ones."""
        if not self._frozen:
            return
        if self._check_layout_integrity():
            # Stale signal from an earlier cycle; current state is intact.
            return
        log("[SmartFreeze] WARN: Tracked widget destroyed; restoring remaining panes.")
        self._do_unfreeze(force_silent=True)

    def _watch_destroyed(self, widget):
        """Connect `widget`'s destroyed() signal once and only once.

        Stacks and DAG containers are long-lived and get frozen over and
        over; without the property tag we would add a fresh connection on
        every single freeze and accumulate thousands of them."""
        if widget is None:
            return
        try:
            if widget.property(self.DESTROY_WATCH_PROPERTY):
                return
            widget.destroyed.connect(self._on_tracked_destroyed)
            widget.setProperty(self.DESTROY_WATCH_PROPERTY, True)
        except (RuntimeError, TypeError):
            pass

    # ---- ORPHAN RECOVERY -------------------------------------------------

    def _recover_stack(self, stack):
        """Remove any DummyPreview widgets from `stack` and restore the
        real widget underneath. Returns True if an orphan was cleared.

        Only ever called when we are not tracking a live freeze on this
        stack, so any preview found here is by definition abandoned."""
        if not is_valid(stack):
            return False

        orphans = []
        real = None
        try:
            for i in range(stack.count()):
                w = stack.widget(i)
                if isinstance(w, DummyPreview):
                    orphans.append(w)
                elif real is None:
                    real = w
        except RuntimeError:
            return False

        if not orphans:
            return False

        if real is not None:
            try:
                stack.setCurrentWidget(real)
            except RuntimeError:
                pass

        for orphan in orphans:
            try:
                stack.removeWidget(orphan)
            except RuntimeError:
                pass
            try:
                orphan.hide()
                orphan.setParent(None)
            except RuntimeError:
                pass
            try:
                orphan.deleteLater()
            except RuntimeError:
                pass

        return True

    def sweep_all_orphans(self):
        """Find every abandoned DummyPreview anywhere in the application
        and clear it. Returns the number of stacks recovered.

        Scans for the previews themselves rather than for DAG widgets: a
        stuck DAG is not the current widget of its stack, so isVisible()
        is False for it and the usual DAG lookup would miss it."""
        recovered = 0
        seen = []
        for w in QtWidgets.QApplication.allWidgets():
            if not isinstance(w, DummyPreview):
                continue
            stack = self._find_stack(w)
            if stack is None:
                continue
            if any(s is stack for s in seen):
                continue
            seen.append(stack)
            if self._recover_stack(stack):
                recovered += 1
        return recovered

    def toggle_freeze(self):
        """Sticky keyboard toggle. Freezes if idle, releases if frozen --
        regardless of which trigger started the current freeze."""
        if self._frozen:
            self._do_unfreeze(force_silent=True)
            log("[SmartFreeze] Unfrozen by keyboard toggle.")
            return False
        self._freeze(sticky=True)
        return self._frozen

    def refresh_previews(self):
        """Repaint any on-screen preview. Called when an appearance pref
        changes so edits show up immediately instead of at next freeze."""
        for item in self._current_dummies:
            dummy = item.get('dummy')
            if is_valid(dummy):
                try:
                    dummy.update()
                except RuntimeError:
                    pass

    def force_unfreeze(self):
        """Manual escape hatch. Clears any wedged guard flag, restores all
        tracked panes, then sweeps the whole application for orphans."""
        self._is_unfreezing = False
        self._do_unfreeze(force_silent=True)
        recovered = self.sweep_all_orphans()
        if recovered:
            log("[SmartFreeze] Force unfreeze recovered %d stack(s)." % recovered)
        else:
            log("[SmartFreeze] Force unfreeze complete; no orphans found.")
        return recovered

    # ---- SLOW SAFETY POLL ------------------------------------------------

    def _slow_poll(self):
        """Runs 4x per second. Catches focus loss and zombie state in case
        destroyed() didn't fire (some teardown paths skip the signal)."""
        if QtWidgets.QApplication.activeWindow() is None:
            if self._frozen:
                self._do_unfreeze(force_silent=True)
                log("[SmartFreeze] WARN: Focus lost; unfrozen to protect layout saves.")
                return

        if self._frozen and not self._check_layout_integrity():
            log("[SmartFreeze] WARN: Workspace swap detected; purging orphaned dummies.")
            self._do_unfreeze(force_silent=True)

    # ---- LOCAL EVENT FILTER ----------------------------------------------

    def eventFilter(self, obj, event):
        """Per-widget event filter installed on target areas only. Listens
        for left-mouse press (freeze trigger) and release (debounced
        unfreeze)."""
        etype = event.type()

        if etype not in (QtCore.QEvent.MouseButtonPress,
                         QtCore.QEvent.MouseButtonRelease):
            return False

        if not _PREF_ENABLED_CACHE:
            return False

        if etype == QtCore.QEvent.MouseButtonPress:
            if event.button() == QtCore.Qt.LeftButton:
                # Already frozen -> the user is starting another scrub.
                # Cancel any pending unfreeze debounce. A sticky freeze
                # has no pending debounce and simply stays put.
                if self._frozen:
                    self._unfreeze_timer.stop()
                elif _PREF_TRIGGER_MODE_CACHE in ("Timeline", "Both"):
                    global_pos = event.globalPos()
                    widget_under_cursor = QtWidgets.QApplication.widgetAt(global_pos)
                    if self._is_target_area(widget_under_cursor, global_pos):
                        self._freeze()

        elif etype == QtCore.QEvent.MouseButtonRelease:
            if event.button() == QtCore.Qt.LeftButton:
                # Sticky freezes are released by the keyboard toggle only.
                if self._frozen and not self._sticky:
                    delay = max(1, _PREF_DELAY_CACHE)
                    self._unfreeze_timer.setInterval(delay)
                    self._unfreeze_timer.start()

        return False

    # ---- FREEZE / UNFREEZE -----------------------------------------------

    def _freeze(self, sticky=False):
        self._unfreeze_timer.stop()
        self._dag_rects = []

        active_stacks = {}
        for gl in self._get_dag_gl_widgets():
            stack = self._find_stack(gl)
            if stack and stack not in active_stacks:
                active_stacks[stack] = gl

        if not active_stacks:
            return

        for stack, gl in active_stacks.items():
            try:
                if isinstance(stack.currentWidget(), DummyPreview):
                    # An abandoned preview from an earlier cycle. Clear it
                    # and restore the real widget. We deliberately do NOT
                    # freeze this stack on the same pass: the DAG has only
                    # just been made current again and its framebuffer is
                    # not reliably drawn yet. The next click freezes it
                    # normally.
                    if self._recover_stack(stack):
                        log("[SmartFreeze] WARN: Orphaned preview cleared; "
                            "node graph restored.")
                    continue
            except RuntimeError:
                continue

            frame = None
            try:
                if hasattr(gl, 'grabFrameBuffer'):
                    frame = gl.grabFrameBuffer()
                elif hasattr(gl, 'grabFramebuffer'):
                    frame = gl.grabFramebuffer()
            except RuntimeError:
                continue

            if frame is None:
                continue

            dummy = DummyPreview(QtGui.QPixmap.fromImage(frame))

            try:
                rect = QtCore.QRect(
                    gl.mapToGlobal(QtCore.QPoint(0, 0)),
                    gl.size()
                )
            except RuntimeError:
                dummy.deleteLater()
                continue

            try:
                freeze_widget = stack.currentWidget()
                stack.addWidget(dummy)
                stack.setCurrentWidget(dummy)
            except RuntimeError:
                dummy.deleteLater()
                continue

            # destroyed() tripwires on the widgets Nuke owns, so we learn
            # about workspace swaps and panel closes.
            #
            # The preview's own destroyed() signal is deliberately NOT
            # connected. We create and delete it ourselves, so the signal
            # carries no information we lack -- and because deleteLater()
            # defers destruction, a preview from the previous cycle could
            # fire it after the next freeze had begun and wipe the new
            # cycle's state. That was the cause of the stuck-frozen DAG.
            self._watch_destroyed(stack)
            self._watch_destroyed(freeze_widget)

            self._dag_rects.append(rect)
            self._current_dummies.append({
                'stack': stack,
                'dummy': dummy,
                'restore': freeze_widget,
            })

        if self._current_dummies:
            self._frozen = True
            self._sticky = sticky
            if sticky:
                log("[SmartFreeze] Frozen by keyboard toggle.")
            else:
                log("[SmartFreeze] Frozen by timeline action.")

    def _do_unfreeze(self, force_silent=False):
        if self._is_unfreezing:
            return
        self._is_unfreezing = True

        # try/finally: if anything below raises, _purge_state must still
        # run. _is_unfreezing is only cleared in there, and a stuck True
        # would make every future unfreeze return at the guard above,
        # wedging the tool in the frozen state permanently.
        try:
            for item in self._current_dummies:
                stack = item.get('stack')
                dummy = item.get('dummy')
                restore = item.get('restore')

                if is_valid(stack) and is_valid(restore):
                    try:
                        stack.setCurrentWidget(restore)
                    except RuntimeError:
                        pass

                if is_valid(dummy):
                    if is_valid(stack):
                        try:
                            stack.removeWidget(dummy)
                        except RuntimeError:
                            pass
                    try:
                        dummy.hide()
                        dummy.setParent(None)
                    except RuntimeError:
                        pass
                    try:
                        dummy.deleteLater()
                    except RuntimeError:
                        pass
        finally:
            self._purge_state()

        if not force_silent:
            log("[SmartFreeze] Unfrozen.")

    # ---- CLEANUP ---------------------------------------------------------

    def cleanup(self):
        try:
            self._slow_timer.stop()
        except Exception:
            pass
        try:
            self._hook_timer.stop()
        except Exception:
            pass
        self._is_unfreezing = False
        self._do_unfreeze(force_silent=True)
        self.sweep_all_orphans()

        # Remove our filter from every widget we previously hooked, and
        # clear the destroy-watch tags so a reloaded instance rewires its
        # own connections instead of assuming ours are still live.
        for w in QtWidgets.QApplication.allWidgets():
            try:
                if w.property(self.HOOK_PROPERTY):
                    w.removeEventFilter(self)
                    w.setProperty(self.HOOK_PROPERTY, False)
            except RuntimeError:
                pass
            try:
                if w.property(self.DESTROY_WATCH_PROPERTY):
                    try:
                        w.destroyed.disconnect(self._on_tracked_destroyed)
                    except (RuntimeError, TypeError):
                        pass
                    w.setProperty(self.DESTROY_WATCH_PROPERTY, False)
            except RuntimeError:
                pass


# --- MANUAL RECOVERY ENTRY POINTS ------------------------------------------

def toggle_freeze():
    """Sticky freeze toggle, bound to the configured keyboard shortcut."""
    if _PREF_TRIGGER_MODE_CACHE not in ("Keyboard", "Both"):
        nuke.message(
            "SmartFreeze: the keyboard trigger is disabled.\n\n"
            "Set Preferences > SmartFreeze > Trigger Mode to "
            "'Keyboard' or 'Both' to use it."
        )
        return
    inst = getattr(nuke, '_viewer_smart_freeze', None)
    if inst is None:
        nuke.message("SmartFreeze is not loaded.")
        return
    inst.toggle_freeze()


def force_unfreeze():
    """Restore the node graph immediately and clear any abandoned preview
    left anywhere in the application. Safe to run at any time."""
    inst = getattr(nuke, '_viewer_smart_freeze', None)
    if inst is None:
        nuke.message("SmartFreeze is not loaded.")
        return
    recovered = inst.force_unfreeze()
    if recovered:
        nuke.message("SmartFreeze: restored %d node graph(s)." % recovered)


def reset_smartfreeze():
    """Full teardown and rebuild. Use when the tool is misbehaving in a
    way Unfreeze Now does not resolve. Rebuilds the instance from
    scratch, then sweeps for anything the old one left behind."""
    inst = getattr(nuke, '_viewer_smart_freeze', None)
    if inst is not None:
        try:
            inst.cleanup()
        except Exception:
            pass
        try:
            del nuke._viewer_smart_freeze
        except Exception:
            pass

    # Rebuild first, then sweep with the fresh instance -- if the old one
    # was wedged, its own sweep may not have completed.
    nuke._viewer_smart_freeze = ViewerSmartFreeze()
    nuke._viewer_smart_freeze.sweep_all_orphans()
    nuke.message("SmartFreeze has been reset.")


def setup_menu():
    """Register recovery commands under a top-level SmartFreeze menu.

    Commands are addressed via __name__ so this still works if the module
    is imported under a different name (SmartFreeze2, etc.)."""
    try:
        shortcut = DEFAULT_SHORTCUT
        try:
            knob = _PREFS.knob("smartfreeze_shortcut")
            if knob and knob.value().strip():
                shortcut = knob.value().strip()
        except Exception:
            pass

        menu = nuke.menu('Nuke').addMenu('SmartFreeze')
        menu.addCommand('Toggle Freeze', '%s.toggle_freeze()' % __name__, shortcut)
        menu.addCommand('Unfreeze Now', '%s.force_unfreeze()' % __name__)
        menu.addCommand('Reset SmartFreeze', '%s.reset_smartfreeze()' % __name__)
    except Exception:
        pass


# --- PREFERENCES SETUP -----------------------------------------------------

def setup_preferences():
    prefs = nuke.toNode("preferences")

    if prefs.knob("SmartFreeze") is None:
        prefs.addKnob(nuke.Tab_Knob("SmartFreeze"))

    if not prefs.knob("smartfreeze_enable"):
        prefs.addKnob(nuke.Text_Knob("smartfreeze_heading", "<h3>SmartFreeze Settings</h3>"))
        prefs.knob("smartfreeze_heading").setFlag(nuke.STARTLINE)

        enable_knob = nuke.Boolean_Knob("smartfreeze_enable", "Enable SmartFreeze")
        enable_knob.setValue(True)
        enable_knob.setFlag(nuke.STARTLINE)
        prefs.addKnob(enable_knob)

        delay_knob = nuke.Int_Knob("smartfreeze_delay", "Unfreeze Delay (ms)")
        delay_knob.setValue(20)
        delay_knob.setFlag(nuke.STARTLINE)
        prefs.addKnob(delay_knob)

        log_knob = nuke.Boolean_Knob("smartfreeze_logging", "Enable Console Logging")
        log_knob.setValue(False)
        log_knob.setFlag(nuke.STARTLINE)
        prefs.addKnob(log_knob)

        # --- Trigger ---
        prefs.addKnob(nuke.Text_Knob("smartfreeze_trigger_div", ""))
        prefs.addKnob(nuke.Text_Knob(
            "smartfreeze_trigger_heading", "<b>Trigger</b>"))
        prefs.knob("smartfreeze_trigger_heading").setFlag(nuke.STARTLINE)

        mode_knob = nuke.Enumeration_Knob(
            "smartfreeze_trigger_mode", "Trigger Mode", TRIGGER_MODES)
        mode_knob.setValue(DEFAULT_TRIGGER_MODE)
        mode_knob.setFlag(nuke.STARTLINE)
        mode_knob.setTooltip(
            "Timeline: freeze while scrubbing the timeline.\n"
            "Keyboard: freeze with the shortcut below (stays frozen "
            "until pressed again).\n"
            "Both: either trigger works.")
        prefs.addKnob(mode_knob)

        shortcut_knob = nuke.String_Knob("smartfreeze_shortcut", "Shortcut")
        shortcut_knob.setValue(DEFAULT_SHORTCUT)
        shortcut_knob.setFlag(nuke.STARTLINE)
        shortcut_knob.setTooltip(
            "Keyboard shortcut for Toggle Freeze. Takes effect after "
            "restarting Nuke.")
        prefs.addKnob(shortcut_knob)

        # --- Frozen state indicator ---
        prefs.addKnob(nuke.Text_Knob("smartfreeze_indicator_div", ""))
        prefs.addKnob(nuke.Text_Knob(
            "smartfreeze_indicator_heading", "<b>Frozen State Indicator</b>"))
        prefs.knob("smartfreeze_indicator_heading").setFlag(nuke.STARTLINE)

        show_outline_knob = nuke.Boolean_Knob(
            "smartfreeze_show_outline", "Show Outline")
        show_outline_knob.setValue(DEFAULT_SHOW_OUTLINE)
        show_outline_knob.setFlag(nuke.STARTLINE)
        prefs.addKnob(show_outline_knob)

        outline_color_knob = nuke.Color_Knob(
            "smartfreeze_outline_color", "Outline Color")
        outline_color_knob.setValue([0.90, 0.62, 0.20])
        outline_color_knob.setFlag(nuke.STARTLINE)
        prefs.addKnob(outline_color_knob)

        outline_width_knob = nuke.Int_Knob(
            "smartfreeze_outline_width", "Outline Width (px)")
        outline_width_knob.setValue(2)
        outline_width_knob.setFlag(nuke.STARTLINE)
        prefs.addKnob(outline_width_knob)

        show_tint_knob = nuke.Boolean_Knob("smartfreeze_show_tint", "Show Tint")
        show_tint_knob.setValue(DEFAULT_SHOW_TINT)
        show_tint_knob.setFlag(nuke.STARTLINE)
        show_tint_knob.setTooltip(
            "Wash the frozen node graph with a colour overlay. Off by "
            "default: the outline alone is usually enough, and a heavy "
            "tint can read as a broken panel.")
        prefs.addKnob(show_tint_knob)

        tint_color_knob = nuke.Color_Knob("smartfreeze_tint_color", "Tint Color")
        tint_color_knob.setValue([0.10, 0.10, 0.10])
        tint_color_knob.setFlag(nuke.STARTLINE)
        prefs.addKnob(tint_color_knob)

        tint_opacity_knob = nuke.Double_Knob(
            "smartfreeze_tint_opacity", "Tint Opacity")
        tint_opacity_knob.setRange(0.0, 1.0)
        tint_opacity_knob.setValue(0.25)
        tint_opacity_knob.setFlag(nuke.STARTLINE)
        prefs.addKnob(tint_opacity_knob)


# --- INITIALIZATION & HOT-RELOAD SAFETY ------------------------------------

setup_preferences()
_update_pref_caches()
setup_menu()

# Register the knobChanged callback so toggling a SmartFreeze pref
# updates the cache immediately on the main thread, without needing
# the slow poll to pick it up.
try:
    nuke.removeKnobChanged(_on_pref_changed, nodeClass="Preferences")
except Exception:
    pass
try:
    nuke.addKnobChanged(_on_pref_changed, nodeClass="Preferences")
except Exception:
    pass

if hasattr(nuke, '_viewer_smart_freeze'):
    try:
        nuke._viewer_smart_freeze.cleanup()
    except Exception:
        pass
    try:
        del nuke._viewer_smart_freeze
    except Exception:
        pass

nuke._viewer_smart_freeze = ViewerSmartFreeze()
