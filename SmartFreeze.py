"""
SmartFreeze for Nuke (v4.0 -- Mouse-Release Debounce Architecture)
===============================================================

A zero-latency performance tool that freezes the Node Graph (DAG) during
timeline scrubbing to eliminate UI lag in heavy Nuke scripts.

v4.0 design notes:
------------------
- UNFREEZE PARADIGM SHIFT: Completely removed the spatial hover (MouseMove) 
  unfreeze logic. The script now listens for MouseButtonRelease events. 
- DEBOUNCE TIMER: When the left mouse button is released, a debounce timer 
  starts. If the user clicks again before the timer expires (rapid scrubbing), 
  the unfreeze is aborted.
- USER PREFERENCES: Added a new setting to the Nuke Preferences UI allowing 
  artists to manually adjust the unfreeze delay (in milliseconds).
"""

import nuke
from Qt import QtCore, QtWidgets, QtGui

try:
    from shiboken2 import isValid as _shiboken_is_valid
except ImportError:
    try:
        from shiboken6 import isValid as _shiboken_is_valid
    except ImportError:
        def _shiboken_is_valid(obj):
            return obj is not None


def is_valid(obj):
    if obj is None:
        return False
    try:
        return _shiboken_is_valid(obj)
    except Exception:
        return False


_PREFS = nuke.toNode("preferences")

# --- CACHED PREFERENCES ---
_PREF_ENABLED_CACHE = True
_PREF_LOGGING_CACHE = False
_PREF_DELAY_CACHE = 20  # Default 20ms debounce

def _update_pref_caches():
    global _PREF_ENABLED_CACHE, _PREF_LOGGING_CACHE, _PREF_DELAY_CACHE
    try:
        en_knob = _PREFS.knob("smartfreeze_enable")
        _PREF_ENABLED_CACHE = en_knob.value() if en_knob else True
        
        log_knob = _PREFS.knob("smartfreeze_logging")
        _PREF_LOGGING_CACHE = log_knob.value() if log_knob else False
        
        delay_knob = _PREFS.knob("smartfreeze_delay")
        if delay_knob:
            _PREF_DELAY_CACHE = int(delay_knob.value())
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


class ViewerSmartFreeze(QtCore.QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._frozen = False
        self._is_unfreezing = False

        self._current_dummies = []
        self._dag_rects = []

        # The timer is no longer hardcoded; it dynamically reads user preferences
        self._unfreeze_timer = QtCore.QTimer(self)
        self._unfreeze_timer.setSingleShot(True)
        self._unfreeze_timer.timeout.connect(self._do_unfreeze)
        
        # --- BACKGROUND SAFETY POLL (250ms) ---
        self._slow_timer = QtCore.QTimer(self)
        self._slow_timer.setInterval(250) 
        self._slow_timer.timeout.connect(self._slow_poll)
        self._slow_timer.start()

        # --- SNIPER HOOK TIMER (1000ms) ---
        self._hook_timer = QtCore.QTimer(self)
        self._hook_timer.setInterval(1000)
        self._hook_timer.timeout.connect(self._refresh_localized_hooks)
        self._hook_timer.start()

        app = QtWidgets.QApplication.instance()
        app.aboutToQuit.connect(self.cleanup)

        log("[SmartFreeze] v8.2 Mouse-Release Debounce Architecture ready.")

    def _apply_hook(self, widget):
        """Attaches the event filter safely using a C++ property tag."""
        try:
            if not widget.property("SmartFreezeHooked"):
                widget.installEventFilter(self)
                widget.setProperty("SmartFreezeHooked", True)
        except RuntimeError:
            pass

    def _refresh_localized_hooks(self):
        """Scans the UI to attach zero-latency filters ONLY to target areas."""
        if not _PREF_ENABLED_CACHE:
            return

        for w in QtWidgets.QApplication.allWidgets():
            try:
                name = w.objectName()
            except RuntimeError:
                continue
                
            if any(x in name for x in ('DAG', 'Viewer', 'DopeSheet', 'CurveEditor')):
                self._apply_hook(w)
                
                try:
                    for child in w.findChildren(QtWidgets.QWidget):
                        self._apply_hook(child)
                except RuntimeError:
                    pass

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
        if not self._frozen:
            return True
        if not self._current_dummies:
            return False
        for item in self._current_dummies:
            if not is_valid(item.get('stack')) or not is_valid(item.get('dummy')):
                return False
        return True

    def _purge_state(self):
        self._current_dummies = []
        self._dag_rects = []
        self._frozen = False
        self._is_unfreezing = False
        self._unfreeze_timer.stop()


    # ---- BACKGROUND SAFETY (Runs 4 times a second) ----
    def _slow_poll(self):
        _update_pref_caches()

        if QtWidgets.QApplication.activeWindow() is None:
            if self._frozen:
                self._do_unfreeze(force_silent=True)
                log("[SmartFreeze] ⚠️ Focus lost; unfrozen to protect layout saves.")

        if self._frozen and not self._check_layout_integrity():
            log("[SmartFreeze] ⚠️ Workspace swap detected; purging orphaned dummies.")
            self._do_unfreeze(force_silent=True)


    # ---- INSTANT LOCAL EVENT FILTER ----
    def eventFilter(self, obj, event):
        etype = event.type()
        
        # We now care about Press AND Release events
        if etype not in (QtCore.QEvent.MouseButtonPress, QtCore.QEvent.MouseButtonRelease):
            return False

        if not _PREF_ENABLED_CACHE:
            return False

        # 1. TRIGGER FREEZE / ABORT UNFREEZE ON CLICK-DOWN
        if etype == QtCore.QEvent.MouseButtonPress:
            if event.button() == QtCore.Qt.LeftButton:
                
                # If we are already frozen, cancel any pending unfreeze debounce timers
                if self._frozen:
                    self._unfreeze_timer.stop()
                
                # Otherwise, check if we need to freeze
                else:
                    global_pos = event.globalPos()
                    widget_under_cursor = QtWidgets.QApplication.widgetAt(global_pos)
                    
                    if self._is_target_area(widget_under_cursor, global_pos):
                        self._freeze()

        # 2. TRIGGER DEBOUNCED UNFREEZE ON RELEASE
        elif etype == QtCore.QEvent.MouseButtonRelease:
            if event.button() == QtCore.Qt.LeftButton:
                if self._frozen:
                    # Pull the user's preferred delay (with a 1ms safety clamp to prevent Qt errors)
                    delay = max(1, _PREF_DELAY_CACHE)
                    self._unfreeze_timer.setInterval(delay)
                    self._unfreeze_timer.start()

        return False 


    # ---- FREEZE / UNFREEZE ------------------------------------------------

    def _freeze(self):
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

            self._dag_rects.append(rect)
            self._current_dummies.append({
                'stack': stack,
                'dummy': dummy,
                'restore': freeze_widget,
            })

        if self._current_dummies:
            self._frozen = True
            log("[SmartFreeze] ❄️ Frozen by timeline action.")

    def _unfreeze(self):
        if not self._frozen:
            return
        self._unfreeze_timer.start()

    def _do_unfreeze(self, force_silent=False):
        if self._is_unfreezing:
            return
        self._is_unfreezing = True

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

        self._purge_state()

        if not force_silent:
            log("[SmartFreeze] ✅ Unfrozen by mouse release debounce.")

    def cleanup(self):
        self._slow_timer.stop()
        self._hook_timer.stop()
        self._do_unfreeze(force_silent=True)
        
        for w in QtWidgets.QApplication.allWidgets():
            try:
                if w.property("SmartFreezeHooked"):
                    w.removeEventFilter(self)
                    w.setProperty("SmartFreezeHooked", False)
            except RuntimeError:
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

        # Added the Unfreeze Delay integer knob
        delay_knob = nuke.Int_Knob("smartfreeze_delay", "Unfreeze Delay (ms)")
        delay_knob.setValue(20)
        delay_knob.setFlag(nuke.STARTLINE)
        prefs.addKnob(delay_knob)

        log_knob = nuke.Boolean_Knob("smartfreeze_logging", "Enable Console Logging")
        log_knob.setValue(False)
        log_knob.setFlag(nuke.STARTLINE)
        prefs.addKnob(log_knob)

_update_pref_caches()


# --- INITIALIZATION & HOT-RELOAD SAFETY ------------------------------------

setup_preferences()

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