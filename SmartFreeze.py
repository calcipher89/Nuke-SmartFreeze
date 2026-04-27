"""
SmartFreeze for Nuke
====================
A zero-latency performance tool that freezes the Node Graph (DAG) during timeline scrubbing 
to eliminate UI lag in heavy Nuke scripts. 

Version History:
----------------
v1.0 - Initial hover-based DAG update freeze.
v1.1 - Upgraded to Left-Click trigger to prevent hotkey interference.
v1.2 - "OpenGL Black Hole" bypass using recursive child widget filtering.
v1.3 - Zero-latency click response by caching DAG widgets on initialization.
v2.0 - Architecture redesign: QStackedWidget Framebuffer swap (Credit: User & Claude). 
       Replaced PySide paint-freezing with a static image swap to completely unblock the UI thread.
v2.1 - Dynamic Group Node support. Removed static stack caching to catch newly created DAG tabs.
v2.2 - Removed forced nuke.frame() evaluation on unfreeze to eliminate lag spikes.
v2.3 - "Ghost Widget" protection. Added robust try/except blocks to catch 'RuntimeError: Internal C++ 
       object already deleted' when Nuke dynamically destroys UI elements like QLineEdits.
v3.0 - Action-Based Trigger. Swapped focusChanged for MouseButtonPress to exclusively trigger 
       freezes when interacting with the Timeline, bypassing the main Viewer canvas using 
       Height and Y-Coordinate heuristics.
v3.1 - Drag Protection & Visibility Checks. Fixed UI tearing in the Dope Sheet by ensuring the 
       DAG is visible before freezing, and preventing accidental unfreezes while LMB is held down.
"""

import nuke
try:
    from PySide6 import QtCore, QtWidgets, QtGui
except ImportError:
    from PySide2 import QtCore, QtWidgets, QtGui

DEBUG = False  # Set to True to enable console logging

def log(msg):
    if DEBUG:
        print(msg)


class DummyPreview(QtWidgets.QLabel):
    def __init__(self, pixmap, parent=None):
        super().__init__(parent)
        self.setPixmap(pixmap)
        self.setScaledContents(True)
        self.setObjectName("SmartFreezeDummy")


class ViewerSmartFreeze(QtCore.QObject):

    def __init__(self, parent=None):
        super().__init__(parent)
        self._frozen = False
        self._dag_rects = {}
        self._current_dummies = []  
        
        self._unfreeze_timer = QtCore.QTimer(self)
        self._unfreeze_timer.setSingleShot(True)
        self._unfreeze_timer.setInterval(80)
        self._unfreeze_timer.timeout.connect(self._do_unfreeze)

        QtWidgets.QApplication.instance().installEventFilter(self)

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

    def eventFilter(self, obj, event):
        # FREEZE TRIGGER
        if event.type() == QtCore.QEvent.MouseButtonPress:
            if event.button() == QtCore.Qt.LeftButton:
                global_pos = event.globalPos()
                widget_under_cursor = QtWidgets.QApplication.widgetAt(global_pos)
                
                if not self._frozen and self._is_target_area(widget_under_cursor, global_pos):
                    self._freeze()

        # UNFREEZE TRIGGER
        elif event.type() == QtCore.QEvent.MouseMove:
            if self._frozen:
                # Drag Protection
                if event.buttons() & QtCore.Qt.LeftButton:
                    return False
                
                if self._cursor_over_dag():
                    self._unfreeze()

        return False 

    def _freeze(self):
        self._unfreeze_timer.stop()
        self._dag_rects.clear()
        
        active_stacks = {}
        for gl in self._get_dag_gl_widgets():
            stack = self._find_stack(gl)
            if stack and stack not in active_stacks:
                active_stacks[stack] = gl

        for stack, gl in active_stacks.items():
            if isinstance(stack.currentWidget(), DummyPreview):
                continue

            try:
                frame = gl.grabFrameBuffer()
            except AttributeError:
                frame = gl.grabFramebuffer()
                
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
            
        self._frozen = True
        log("[SmartFreeze] ❄️  Frozen by Timeline Action")

    def _unfreeze(self):
        if not self._frozen:
            return
        self._unfreeze_timer.start()

    def _do_unfreeze(self):
        for item in self._current_dummies:
            stack = item['stack']
            dummy = item['dummy']
            restore_widget = item['restore_widget']

            try:
                stack.setCurrentWidget(restore_widget)
            except RuntimeError:
                pass
            
            stack.removeWidget(dummy)
            dummy.deleteLater()
            
        self._current_dummies.clear()
        self._dag_rects.clear()
        self._frozen = False
        log("[SmartFreeze] ✅  Unfrozen by DAG Hover")

    def cleanup(self):
        QtWidgets.QApplication.instance().removeEventFilter(self)
        self._unfreeze_timer.stop()
        
        for item in self._current_dummies:
            stack = item['stack']
            dummy = item['dummy']
            restore_widget = item['restore_widget']
            try:
                stack.setCurrentWidget(restore_widget)
            except RuntimeError:
                pass
            stack.removeWidget(dummy)
            dummy.deleteLater()
            
        self._current_dummies.clear()


# --- HOT-RELOAD SAFETY ---
if hasattr(nuke, '_viewer_smart_freeze'):
    nuke._viewer_smart_freeze.cleanup()
    del nuke._viewer_smart_freeze

nuke._viewer_smart_freeze = ViewerSmartFreeze()