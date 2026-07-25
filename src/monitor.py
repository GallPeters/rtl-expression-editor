# -*- coding: utf-8 -*-

"""
Dialog monitor.

This module watches QGIS for supported dialogs and creates one synchronization
controller for each detected dialog/editor pair.

This version also:
- ignores menus, tooltips, splash screens and the main QGIS window
- avoids repeated log spam when controller creation fails
- provides a manual scan entry point
"""

from PyQt6.QtCore import QObject, QEvent, QTimer, Qt
from PyQt6.QtWidgets import QApplication, QDialog, QWidget

from .analyzer import analyze_dialog, quick_dialog_hint
from .controller import EditorSyncController
from .utils import (
    is_deleted,
    is_companion_widget,
    log_exception,
    log_message,
    class_name,
)


_IGNORE_CLASS_PREFIXES = (
    "qmenu",
    "qtooltip",
    "qsplashscreen",
    "qballoon",
    "qpopup",
    "qcombobox",
    "qmessagebox",
)

_MAIN_WINDOW_CLASSES = (
    "qgisapp",
    "qmainwindow",
)


def _should_ignore_widget(widget):
    """Return True for widget types that should never receive a companion."""
    if is_deleted(widget) or not isinstance(widget, QWidget):
        return True

    name = class_name(widget).lower()

    if not name:
        return False

    return name.startswith(_IGNORE_CLASS_PREFIXES)


def _is_main_window_widget(widget):
    """Return True when the widget looks like the main QGIS window."""
    if is_deleted(widget) or not isinstance(widget, QWidget):
        return False

    name = class_name(widget).lower()
    return name.startswith(_MAIN_WINDOW_CLASSES)


class RtlEditorMonitor(QObject):
    """
    Monitors QGIS top-level widgets and creates companion editors.
    """

    def __init__(self, iface=None, parent=None):
        super().__init__(parent)

        self._iface = iface
        self._enabled = False

        self._controllers = {}
        self._watched = set()
        self._non_targets = set()
        self._failed = {}
        self._controller_failures = {}
        self._pending = set()

        self._main_window = None

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(500)
        self._poll_timer.timeout.connect(self._scan_top_level)

        self._pending_timer = QTimer(self)
        self._pending_timer.setSingleShot(True)
        self._pending_timer.setInterval(0)
        self._pending_timer.timeout.connect(self._process_pending)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Start monitoring."""
        if self._enabled:
            return

        self._enabled = True

        try:
            self._main_window = (
                self._iface.mainWindow() if self._iface is not None else None
            )
        except Exception:
            self._main_window = None

        if self._main_window is not None and not is_deleted(self._main_window):
            try:
                self._main_window.installEventFilter(self)
            except Exception as e:
                log_exception("RtlEditorMonitor.start.installEventFilter", e)

        self._poll_timer.start()
        self._scan_top_level()

    def stop(self):
        """Stop monitoring and close all companions."""
        if not self._enabled:
            return

        self._enabled = False

        try:
            self._poll_timer.stop()
        except Exception:
            pass

        try:
            self._pending_timer.stop()
        except Exception:
            pass

        self._pending.clear()

        for controller in list(self._controllers.values()):
            try:
                controller.shutdown()
            except Exception as e:
                log_exception("RtlEditorMonitor.stop.controller.shutdown", e)

        self._controllers.clear()

        for widget in list(self._watched):
            self._unwatch_dialog(widget)

        self._watched.clear()
        self._non_targets.clear()
        self._failed.clear()
        self._controller_failures.clear()

        if self._main_window is not None and not is_deleted(self._main_window):
            try:
                self._main_window.removeEventFilter(self)
            except Exception:
                pass

        self._main_window = None

    # ------------------------------------------------------------------
    # Manual scan
    # ------------------------------------------------------------------

    def scan_now(self, force=True):
        """
        Manually scan all top-level windows.

        This is useful when automatic detection misses a dialog and for
        diagnosing class-name changes between QGIS versions.
        """
        if not self._enabled:
            self.start()

        if force:
            self._non_targets.clear()
            self._failed.clear()
            self._controller_failures.clear()

        self._scan_top_level(force=force, debug=True)

    # ------------------------------------------------------------------
    # Event filter
    # ------------------------------------------------------------------

    def eventFilter(self, obj, event):
        """
        Event filter for the QGIS main window and watched candidate dialogs.
        """
        if not self._enabled or is_deleted(obj):
            return False

        try:
            if not isinstance(obj, QWidget):
                return False

            event_type = event.type()

            if event_type == QEvent.Type.ChildAdded:
                if obj in self._watched:
                    # A child was added to a candidate dialog. The embedded
                    # editor may have just been created.
                    self._non_targets.discard(obj)
                    self._failed.pop(obj, None)
                    self._schedule_attach(obj)

                if obj is self._main_window:
                    child = event.child() if hasattr(event, "child") else None

                    if (
                        child is not None
                        and isinstance(child, QWidget)
                        and not _should_ignore_widget(child)
                        and not _is_main_window_widget(child)
                    ):
                        try:
                            if child.isWindow():
                                if (
                                    isinstance(child, QDialog)
                                    or bool(child.windowFlags() & Qt.WindowType.Dialog)
                                    or quick_dialog_hint(child)
                                ):
                                    self._watch_dialog(child)
                                    self._schedule_attach(child)
                        except Exception:
                            pass

            elif event_type == QEvent.Type.Show:
                if obj in self._watched:
                    self._schedule_attach(obj)

        except Exception as e:
            log_exception("RtlEditorMonitor.eventFilter", e)

        return False

    # ------------------------------------------------------------------
    # Top-level scanning
    # ------------------------------------------------------------------

    def _scan_top_level(self, force=False, debug=False):
        """
        Periodically scan top-level widgets.
        """
        if not self._enabled:
            return

        app = QApplication.instance()
        if app is None:
            return

        try:
            widgets = app.topLevelWidgets()
        except Exception:
            return

        scheduled = 0

        try:
            popup_splash_flags = (
                Qt.WindowType.Popup | Qt.WindowType.SplashScreen
            )
        except AttributeError:
            popup_splash_flags = Qt.WindowType.SplashScreen

        for widget in widgets:
            if is_deleted(widget):
                continue

            if is_companion_widget(widget):
                continue

            if widget in self._controllers:
                continue

            if not force and widget in self._non_targets:
                continue

            try:
                if not widget.isVisible():
                    continue

                if widget is self._main_window:
                    continue
            except Exception:
                continue

            if _should_ignore_widget(widget):
                self._non_targets.add(widget)
                continue

            if _is_main_window_widget(widget):
                self._non_targets.add(widget)
                continue

            try:
                flags = widget.windowFlags()
            except Exception:
                continue

            try:
                if bool(flags & popup_splash_flags):
                    continue
            except Exception:
                pass

            is_dialog = isinstance(widget, QDialog) or bool(
                flags & Qt.WindowType.Dialog
            )
            is_window = bool(flags & Qt.WindowType.Window)

            if is_dialog:
                scheduled += 1
                if debug:
                    self._maybe_attach(widget, debug=True)
                else:
                    self._schedule_attach(widget)

            elif is_window and (force or quick_dialog_hint(widget)):
                scheduled += 1
                if debug:
                    self._maybe_attach(widget, debug=True)
                else:
                    self._schedule_attach(widget)

        if debug:
            log_message(
                f"Manual scan finished: {scheduled} candidate top-level windows examined."
            )

    def _schedule_attach(self, widget):
        """Schedule analysis of a widget on the next event loop iteration."""
        if not self._enabled or is_deleted(widget):
            return

        self._pending.add(widget)

        if not self._pending_timer.isActive():
            self._pending_timer.start()

    def _process_pending(self):
        """Process pending widgets."""
        pending = list(self._pending)
        self._pending.clear()

        for widget in pending:
            if is_deleted(widget):
                continue

            try:
                self._maybe_attach(widget)
            except Exception as e:
                log_exception("RtlEditorMonitor._process_pending", e)

    # ------------------------------------------------------------------
    # Attachment
    # ------------------------------------------------------------------

    def _maybe_attach(self, widget, debug=False):
        """Analyze a widget and attach a companion if appropriate."""
        if not self._enabled or is_deleted(widget):
            return

        if is_companion_widget(widget):
            return

        if widget in self._controllers:
            return

        if widget in self._non_targets:
            return

        if widget is self._main_window:
            self._non_targets.add(widget)
            return

        if _should_ignore_widget(widget):
            self._non_targets.add(widget)
            return

        if _is_main_window_widget(widget):
            self._non_targets.add(widget)
            return

        try:
            if not widget.isVisible():
                return
        except Exception:
            return

        info = None

        try:
            info = analyze_dialog(widget)
        except Exception as e:
            log_exception("RtlEditorMonitor._maybe_attach.analyze_dialog", e)

        if info is not None:
            try:
                controller = EditorSyncController(info, parent=self)
                controller.controllerClosed.connect(self._on_controller_closed)

                self._controllers[widget] = controller

                self._unwatch_dialog(widget)
                self._non_targets.discard(widget)
                self._failed.pop(widget, None)
                self._controller_failures.pop(widget, None)

                try:
                    log_message(
                        "Attached companion editor to "
                        f"{class_name(widget)} - {widget.windowTitle()}"
                    )
                except Exception:
                    pass

            except Exception as e:
                log_exception("RtlEditorMonitor._maybe_attach.controller", e)

                count = self._controller_failures.get(widget, 0) + 1
                self._controller_failures[widget] = count

                if count >= 3:
                    self._non_targets.add(widget)

                    try:
                        log_message(
                            "Stopped retrying companion creation for "
                            f"{class_name(widget)} - {widget.windowTitle()} "
                            "after repeated controller failures."
                        )
                    except Exception:
                        pass
        else:
            self._handle_attach_failed(widget)

    def _handle_attach_failed(self, widget):
        """
        Handle a dialog that is not currently attachable.

        We keep watching for a limited number of attempts because some dialogs
        create their editor widgets lazily.
        """
        try:
            is_dialog = isinstance(widget, QDialog) or bool(
                widget.windowFlags() & Qt.WindowType.Dialog
            )
        except Exception:
            is_dialog = False

        if not is_dialog:
            self._non_targets.add(widget)
            return

        count = self._failed.get(widget, 0) + 1
        self._failed[widget] = count

        self._watch_dialog(widget)

        if count == 1:
            try:
                log_message(
                    "Dialog seen but no supported editor yet: "
                    f"{class_name(widget)} - {widget.windowTitle()}"
                )
            except Exception:
                pass

        if count >= 20:
            self._non_targets.add(widget)

            try:
                log_message(
                    "Stopped watching unsupported dialog: "
                    f"{class_name(widget)} - {widget.windowTitle()}"
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Watching helpers
    # ------------------------------------------------------------------

    def _watch_dialog(self, widget):
        """Install an event filter on a candidate dialog."""
        if is_deleted(widget) or widget in self._watched:
            return

        try:
            widget.installEventFilter(self)
            widget.destroyed.connect(
                lambda *args, w=widget: self._unwatch_dialog(w)
            )
            self._watched.add(widget)
        except Exception as e:
            log_exception("RtlEditorMonitor._watch_dialog", e)

    def _unwatch_dialog(self, widget):
        """Remove event filter from a candidate dialog."""
        if widget is None:
            return

        self._watched.discard(widget)
        self._failed.pop(widget, None)
        self._non_targets.discard(widget)

        if not is_deleted(widget):
            try:
                widget.removeEventFilter(self)
            except Exception:
                pass

            # Do not disconnect destroyed() globally. Other code may be
            # connected to the same signal.

    # ------------------------------------------------------------------
    # Controller cleanup
    # ------------------------------------------------------------------

    def _on_controller_closed(self, controller):
        """Remove a closed controller from the registry."""
        for key, value in list(self._controllers.items()):
            if value is controller:
                self._controllers.pop(key, None)
                break