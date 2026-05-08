"""
SmartFreeze for Nuke
====================
A zero-latency performance tool that freezes the Node Graph (DAG) during timeline scrubbing 
to eliminate UI lag in heavy Nuke scripts. 

Version History:
----------------
[... previous history truncated for brevity ...]
v3.7 - LAYOUT CORRUPTION FIX: Prevented dummy widget from being serialized into Nuke's uistate.ini.
v3.8 - UI STATE SYNC: Instantly unfreezes when target widgets receive Hide or Close events.
v3.9 - GHOST TAB FIX: Aggressively orphaned the dummy widget during unfreeze (setParent(None)) 
       to prevent Nuke's layout manager from spawning empty tabs during rapid workspace swaps.
"""

import nuke
from Qt import QtCore, QtWidgets, QtGui

_PREFS = nuke.toNode("preferences")

def is_smartfreeze_enabled():
    k = _PREFS.knob("smartfreeze_enable")
    return k.value() if k else True

def is_logging_enabled():
    k = _PREFS.knob("smartfreeze_logging")
    return k.value() if k else False

def log(msg):
    if is_logging_enabled():
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
        self._monitored_ids = set()  
        
        self._dag_rects = {}
        self._current_dummies = []  
        
        self._unfreeze_timer = QtCore.QTimer(self)
        self._unfreeze_timer.setSingleShot(True)
        self._unfreeze_timer.setInterval(80)
        self._unfreeze_timer.timeout.connect(self._do_unfreeze)

        app = QtWidgets.QApplication.instance()
        app.installEventFilter(self)
        app.aboutToQuit.connect(self.cleanup)

        log("[SmartFreeze] Action-Based Trigger Ready.")

    def _find_stack(self, widget):
        w = widget.parent()
        while w:
            if isinstance(w, QtWidgets.QStackedWidget):
                return w
            w = w.parent()
        return None

    def _get_dag_gl_widgets(self):
        gl_widgets = []
        for w in QtWidgets.QApplication.allWidgets():
            if w.objectName().startswith('DAG') and isinstance(w, QtWidgets.QWidget):
                if w.isVisible() and (hasattr(w, 'grabFrameBuffer') or hasattr(w, 'grabFramebuffer')):
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

    def _cursor_over_dag(self):
        cursor_pos = QtGui.QCursor.pos()
        for rect in self._dag_rects.values():
            if rect.contains(cursor_pos):
                return True
        return False

    def _check_layout_integrity(self):
        if not self._frozen:
            return True
            
        if not self._current_dummies:
            return False
            
        for item in self._current_dummies:
            try:
                item['dummy'].parent()
            except RuntimeError:
                return False
        return True

    def eventFilter(self, obj, event):
        if not is_smartfreeze_enabled():
            if self._frozen:
                self._do_unfreeze()
            return False

        if event.type() == QtCore.QEvent.ApplicationDeactivate:
            if self._frozen:
                self._do_unfreeze(force_silent=True)
                log("[SmartFreeze] ⚠️ Focus lost. Unfrozen to protect layout saves.")
            return False

        if self._frozen and not self._is_unfreezing:
            if event.type() in (QtCore.QEvent.Hide, QtCore.QEvent.Close):
                if id(obj) in self._monitored_ids:
                    self._do_unfreeze(force_silent=True)
                    log("[SmartFreeze] ⚠️ UI layout change detected. Syncing unfreeze state.")
                    return False

        if self._frozen and event.type() in (QtCore.QEvent.MouseButtonPress, QtCore.QEvent.MouseMove):
            if not self._check_layout_integrity():
                self._current_dummies.clear()
                self._dag_rects.clear()
                self._monitored_ids.clear()
                self._frozen = False
                self._unfreeze_timer.stop()
                log("[SmartFreeze] ⚠️ Zombie state purged via integrity check.")

        if event.type() == QtCore.QEvent.MouseButtonPress:
            if event.button() == QtCore.Qt.LeftButton:
                global_pos = event.globalPos()
                widget_under_cursor = QtWidgets.QApplication.widgetAt(global_pos)
                
                if not self._frozen and self._is_target_area(widget_under_cursor, global_pos):
                    self._freeze()

        elif event.type() == QtCore.QEvent.MouseMove:
            if self._frozen:
                if event.buttons() & QtCore.Qt.LeftButton:
                    return False
                
                if self._cursor_over_dag():
                    self._unfreeze()

        return False 

    def _freeze(self):
        self._unfreeze_timer.stop()
        self._dag_rects.clear()
        self._monitored_ids.clear()
        
        active_stacks = {}
        for gl in self._get_dag_gl_widgets():
            stack = self._find_stack(gl)
            if stack and stack not in active_stacks:
                active_stacks[stack] = gl

        if not active_stacks:
            return

        for stack, gl in active_stacks.items():
            if isinstance(stack.currentWidget(), DummyPreview):
                continue

            frame = None
            if hasattr(gl, 'grabFrameBuffer'):
                frame = gl.grabFrameBuffer()
            elif hasattr(gl, 'grabFramebuffer'):
                frame = gl.grabFramebuffer()
            
            if frame is None:
                continue
                
            dummy = DummyPreview(QtGui.QPixmap.fromImage(frame))
            
            self._dag_rects[gl] = QtCore.QRect(
                gl.mapToGlobal(QtCore.QPoint(0, 0)),
                gl.size()
            )
            
            freeze_widget = stack.currentWidget()
            
            stack.addWidget(dummy)
            stack.setCurrentWidget(dummy)
            
            self._current_dummies.append({
                'stack': stack,
                'dummy': dummy,
                'restore_widget': freeze_widget
            })
            
            self._monitored_ids.add(id(dummy))
            self._monitored_ids.add(id(stack))
            
        self._frozen = True
        log("[SmartFreeze] ❄️  Frozen by Timeline Action")

    def _unfreeze(self):
        if not self._frozen:
            return
        self._unfreeze_timer.start()

    def _do_unfreeze(self, force_silent=False):
        self._is_unfreezing = True
        
        for item in self._current_dummies:
            stack = item['stack']
            dummy = item['dummy']
            restore_widget = item['restore_widget']

            try:
                stack.setCurrentWidget(restore_widget)
            except RuntimeError:
                pass
            
            try:
                stack.removeWidget(dummy)
                # v3.9 Fix: Instantly orphan the widget from Nuke's UI tree 
                # before the Qt trash collector takes over.
                dummy.hide()
                dummy.setParent(None)
            except RuntimeError:
                pass
                
            try:
                dummy.deleteLater()
            except RuntimeError:
                pass
            
        self._current_dummies.clear()
        self._dag_rects.clear()
        self._monitored_ids.clear()
        self._frozen = False
        self._is_unfreezing = False
        
        if not force_silent:
            log("[SmartFreeze] ✅  Unfrozen by DAG Hover")

    def cleanup(self):
        QtWidgets.QApplication.instance().removeEventFilter(self)
        self._unfreeze_timer.stop()
        self._do_unfreeze(force_silent=True)


# --- PREFERENCES SETUP ---
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
        
        log_knob = nuke.Boolean_Knob("smartfreeze_logging", "Enable Console Logging")
        log_knob.setValue(False)
        log_knob.setFlag(nuke.STARTLINE)
        prefs.addKnob(log_knob)


# --- INITIALIZATION & HOT-RELOAD SAFETY ---
setup_preferences()

if hasattr(nuke, '_viewer_smart_freeze'):
    nuke._viewer_smart_freeze.cleanup()
    del nuke._viewer_smart_freeze

nuke._viewer_smart_freeze = ViewerSmartFreeze()