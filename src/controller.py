# -*- coding: utf-8 -*-

"""
Synchronization controller.

One EditorSyncController exists for each detected original dialog/editor pair.

Responsibilities:

- create the companion editor
- read initial text from QScintilla
- synchronize original -> companion
- synchronize companion -> original
- apply changes back to QGIS in a way that triggers normal QGIS signals
- restore the original text when the companion is cancelled
- close automatically when the original dialog closes
- avoid infinite synchronization loops
"""

from PyQt6.QtCore import (
    QObject,
    QEvent,
    QTimer,
    Qt,
    QCoreApplication,
    pyqtSignal,
)
from PyQt6.QtGui import QFocusEvent
from PyQt6.QtWidgets import QDialog, QWidget

from .companion_dialog import CompanionEditorDialog
from .utils import (
    is_deleted,
    log_message,
    log_exception,
    get_widget_text,
    find_first_descendant_by_class_substrings,
    WARNING_LEVEL,
)


class EditorSyncController(QObject):
    """
    Controller for one original QScintilla editor and one companion Qt editor.
    """

    controllerClosed = pyqtSignal(QObject)

    def __init__(self, info, parent=None):
        super().__init__(parent)

        self._closed = False
        self._in_companion_closed = False

        self._info = info
        self._dialog = info.dialog
        self._kind = info.kind
        self._text_source = info.text_source
        self._sci = info.sci

        # Synchronization guards.
        self._suppress_original_count = 0
        self._suppress_qt_count = 0

        # Cancel support.
        #
        # Live synchronization writes companion changes into the original
        # editor. Cancel therefore restores the last baseline text.
        self._dirty = False
        self._baseline_text = ""
        self._last_original_text = ""

        # Pending companion -> original synchronization.
        self._qt_pending_text = None

        self._original_signal_connections = []
        self._companion = None

        # Coalesces original-editor change notifications.
        self._sync_timer = QTimer(self)
        self._sync_timer.setSingleShot(True)
        self._sync_timer.setInterval(0)
        self._sync_timer.timeout.connect(self._sync_from_original)

        # Coalesces companion-editor change notifications.
        self._qt_sync_timer = QTimer(self)
        self._qt_sync_timer.setSingleShot(True)
        self._qt_sync_timer.setInterval(0)
        self._qt_sync_timer.timeout.connect(self._flush_qt_to_original)

        # Timers used to release synchronization guards after direct and
        # queued signal delivery.
        self._suppress_original_timer = QTimer(self)
        self._suppress_original_timer.setSingleShot(True)
        self._suppress_original_timer.setInterval(0)
        self._suppress_original_timer.timeout.connect(
            self._decrement_suppress_original
        )

        self._suppress_qt_timer = QTimer(self)
        self._suppress_qt_timer.setSingleShot(True)
        self._suppress_qt_timer.setInterval(0)
        self._suppress_qt_timer.timeout.connect(self._decrement_suppress_qt)

        self._initialize()

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def _initialize(self):
        """Create companion window and connect signals."""
        initial = self._read_original_text()
        if initial is None:
            raise RuntimeError("Unable to read text from the original editor.")

        self._baseline_text = initial
        self._last_original_text = initial

        self._companion = CompanionEditorDialog(
            self._dialog,
            self._info.title,
            self._info.settings_key,
        )

        self._companion.set_text(initial)

        # If the original editor is read-only, mirror that state.
        read_only = False
        try:
            if (
                self._sci is not None
                and not is_deleted(self._sci)
                and hasattr(self._sci, "isReadOnly")
            ):
                read_only = bool(self._sci.isReadOnly())
            elif (
                self._text_source is not None
                and not is_deleted(self._text_source)
                and hasattr(self._text_source, "isReadOnly")
            ):
                read_only = bool(self._text_source.isReadOnly())
        except Exception:
            read_only = False

        if read_only:
            self._companion.editor.setReadOnly(True)

        self._companion.editor.textChanged.connect(self._on_qt_text_changed)
        self._companion.applyRequested.connect(self.apply_to_original)
        self._companion.okRequested.connect(self._ok_requested)
        self._companion.cancelRequested.connect(self._cancel_requested)
        self._companion.closed.connect(self._companion_closed)

        self._connect_original_signals()
        self._install_event_filters()

        self._companion.show()

    def _connect_original_signals(self):
        """
        Connect to original editor change signals when available.

        QScintilla and QGIS code-editor wrappers usually expose textChanged().
        If not, the event filter fallback still checks for changes after
        relevant user-interaction events.
        """
        connected = False
        seen = set()

        # Prefer the wrapper/text source, then the raw Scintilla widget.
        for obj in (self._text_source, self._sci):
            if obj is None or is_deleted(obj):
                continue

            obj_id = id(obj)
            if obj_id in seen:
                continue
            seen.add(obj_id)

            signal = getattr(obj, "textChanged", None)
            connector = getattr(signal, "connect", None)

            if callable(connector):
                try:
                    connector(self._on_original_signal)
                    self._original_signal_connections.append((obj, signal))
                    connected = True
                except Exception as e:
                    log_exception(
                        "EditorSyncController._connect_original_signals.textChanged",
                        e,
                    )

        # Fallback: modificationChanged() may exist when textChanged() does not.
        if not connected:
            for obj in (self._text_source, self._sci):
                if obj is None or is_deleted(obj):
                    continue

                obj_id = id(obj)
                if obj_id in seen:
                    continue
                seen.add(obj_id)

                signal = getattr(obj, "modificationChanged", None)
                connector = getattr(signal, "connect", None)

                if callable(connector):
                    try:
                        connector(self._on_original_signal)
                        self._original_signal_connections.append((obj, signal))
                        connected = True
                    except Exception as e:
                        log_exception(
                            "EditorSyncController._connect_original_signals.modificationChanged",
                            e,
                        )

        if not connected:
            log_message(
                "Original editor has no usable textChanged signal; "
                "using event-driven fallback.",
                WARNING_LEVEL,
            )

    def _install_event_filters(self):
        """Install filters on the original dialog and editor widgets."""
        seen = set()

        for obj in (self._dialog, self._sci, self._text_source):
            if obj is None or is_deleted(obj):
                continue

            obj_id = id(obj)
            if obj_id in seen:
                continue
            seen.add(obj_id)

            try:
                obj.installEventFilter(self)
            except Exception as e:
                log_exception(
                    "EditorSyncController._install_event_filters.install",
                    e,
                )

            if obj is self._dialog:
                try:
                    obj.destroyed.connect(self._on_dialog_destroyed)
                except Exception as e:
                    log_exception(
                        "EditorSyncController._install_event_filters.destroyed",
                        e,
                    )

                if isinstance(obj, QDialog):
                    try:
                        obj.finished.connect(self._on_dialog_finished)
                    except Exception as e:
                        log_exception(
                            "EditorSyncController._install_event_filters.finished",
                            e,
                        )

    # ------------------------------------------------------------------
    # Event filtering
    # ------------------------------------------------------------------

    def eventFilter(self, obj, event):
        """
        Watch the original dialog and original editor.

        This is used for:

        - closing the companion when the original dialog closes
        - raising the companion when the original dialog is activated
        - fallback original-editor change detection
        """
        if self._closed or is_deleted(obj):
            return False

        try:
            event_type = event.type()

            if obj is self._dialog:
                if event_type == QEvent.Type.Close and not isinstance(
                    self._dialog, QDialog
                ):
                    self._close_for_original()
                elif event_type == QEvent.Type.Hide:
                    QTimer.singleShot(0, self._check_dialog_visibility)
                elif event_type in (
                    QEvent.Type.Show,
                    QEvent.Type.WindowActivate,
                ):
                    self._raise_companion_deferred()

            elif obj is self._sci or obj is self._text_source:
                if event_type in (
                    QEvent.Type.KeyRelease,
                    QEvent.Type.InputMethod,
                    QEvent.Type.MouseButtonRelease,
                    QEvent.Type.Drop,
                    QEvent.Type.FocusOut,
                ):
                    self._schedule_original_check()

        except Exception as e:
            log_exception("EditorSyncController.eventFilter", e)

        return False

    def _on_dialog_destroyed(self, *args):
        """Original dialog C++ object was destroyed."""
        self._close_for_original()

    def _on_dialog_finished(self, *args):
        """QDialog finished signal."""
        self._close_for_original()

    def _check_dialog_visibility(self):
        """Close companion if the original dialog is no longer visible."""
        if self._closed:
            return

        if is_deleted(self._dialog):
            self._close_for_original()
            return

        try:
            if not self._dialog.isVisible():
                self._close_for_original()
        except Exception:
            self._close_for_original()

    def _raise_companion_deferred(self):
        """Raise companion without stealing focus."""
        if self._closed or self._companion is None or is_deleted(self._companion):
            return

        try:
            if self._companion.isVisible():
                QTimer.singleShot(0, self._raise_companion)
        except Exception:
            pass

    def _raise_companion(self):
        """Raise companion window."""
        if self._closed or self._companion is None or is_deleted(self._companion):
            return

        try:
            if self._companion.isVisible():
                self._companion.raise_()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Original -> companion synchronization
    # ------------------------------------------------------------------

    def _on_original_signal(self, *args):
        """Slot for original editor signals."""
        if self._closed or self._suppress_original_count > 0:
            return

        self._schedule_original_check()

    def _schedule_original_check(self):
        """Coalesce multiple original-editor notifications."""
        if self._closed:
            return

        if not self._sync_timer.isActive():
            self._sync_timer.start()

    def _sync_from_original(self):
        """Read original text and update the companion editor."""
        if self._closed:
            return

        if self._suppress_original_count > 0:
            self._schedule_original_check()
            return

        text = self._read_original_text()
        if text is None:
            return

        if text == self._last_original_text:
            return

        # External original change cancels pending companion writes.
        self._qt_pending_text = None
        self._qt_sync_timer.stop()

        self._last_original_text = text
        self._baseline_text = text
        self._dirty = False

        self._set_qt_text(text)

    def _set_qt_text(self, text):
        """Update companion text without triggering companion -> original sync."""
        if self._closed or self._companion is None or is_deleted(self._companion):
            return

        if self._companion.text() == text:
            return

        self._begin_suppress_qt()

        try:
            self._companion.set_text_preserve_cursor(text)
        except Exception as e:
            log_exception("EditorSyncController._set_qt_text", e)

    # ------------------------------------------------------------------
    # Companion -> original synchronization
    # ------------------------------------------------------------------

    def _on_qt_text_changed(self):
        """Companion text changed."""
        if self._closed or self._suppress_qt_count > 0:
            return

        if self._companion is None or is_deleted(self._companion):
            return

        text = self._companion.text()

        if text == self._last_original_text:
            self._qt_pending_text = None
            self._qt_sync_timer.stop()
            return

        if not self._dirty:
            current = self._read_original_text()
            if current is not None:
                self._baseline_text = current
            self._dirty = True

        self._qt_pending_text = text

        if not self._qt_sync_timer.isActive():
            self._qt_sync_timer.start()

    def _flush_qt_to_original(self):
        """Write pending companion text to the original editor."""
        if self._closed:
            return

        text = self._qt_pending_text
        self._qt_pending_text = None

        if text is None:
            return

        if self._suppress_qt_count > 0:
            return

        if text == self._last_original_text:
            return

        ok = self._write_original_text(text, force=False)
        if ok:
            self._last_original_text = text

    # ------------------------------------------------------------------
    # Reading and writing the original editor
    # ------------------------------------------------------------------

    def _read_original_text(self):
        """
        Read text from the original editor.

        Prefer the raw Scintilla widget when available because it is the
        authoritative text store.
        """
        seen = set()

        for obj in (self._sci, self._text_source):
            if obj is None or is_deleted(obj):
                continue

            obj_id = id(obj)
            if obj_id in seen:
                continue
            seen.add(obj_id)

            text = get_widget_text(obj)
            if text is not None:
                return text

        return None

    def _write_original_text(self, text, force=False):
        """
        Write text into the original QScintilla editor.

        When possible, this simulates an editing operation by selecting all
        text and replacing the selection. This causes Scintilla to emit its
        normal modification notifications.
        """
        if self._closed:
            return False

        if text is None:
            text = ""

        text = str(text)

        current = self._read_original_text()
        if current is None:
            return False

        if not force and current == text:
            return True

        self._begin_suppress_original()
        done = False

        try:
            sci = self._sci

            if sci is not None and not is_deleted(sci):
                select_all = getattr(sci, "selectAll", None)
                replace_selected_text = getattr(sci, "replaceSelectedText", None)

                if callable(select_all) and callable(replace_selected_text):
                    try:
                        select_all()
                        replace_selected_text(text)
                        done = True
                    except Exception as e:
                        log_exception(
                            "EditorSyncController._write_original_text.replaceSelectedText",
                            e,
                        )

                if not done:
                    setter = getattr(sci, "setText", None)
                    if callable(setter):
                        try:
                            setter(text)
                            done = True
                        except Exception as e:
                            log_exception(
                                "EditorSyncController._write_original_text.sci.setText",
                                e,
                            )

            if not done:
                source = self._text_source
                if source is not None and not is_deleted(source):
                    setter = getattr(source, "setText", None)
                    if callable(setter):
                        try:
                            setter(text)
                            done = True
                        except Exception as e:
                            log_exception(
                                "EditorSyncController._write_original_text.text_source.setText",
                                e,
                            )

            if not done:
                return False

            # Encourage QGIS internals to react even if a wrapper does not
            # re-emit Scintilla notifications.
            self._emit_text_changed(self._sci)
            if self._text_source is not self._sci:
                self._emit_text_changed(self._text_source)

            return True

        except Exception as e:
            log_exception("EditorSyncController._write_original_text", e)
            return False

    def _emit_text_changed(self, obj):
        """Emit textChanged() on an object when possible."""
        if obj is None or is_deleted(obj):
            return

        signal = getattr(obj, "textChanged", None)
        emitter = getattr(signal, "emit", None)

        if callable(emitter):
            try:
                emitter()
            except TypeError:
                # Signal signature mismatch; ignore.
                pass
            except Exception as e:
                log_exception("EditorSyncController._emit_text_changed", e)

    # ------------------------------------------------------------------
    # Apply / OK / Cancel
    # ------------------------------------------------------------------

    def apply_to_original(self):
        """
        Apply companion text to the original editor and trigger QGIS-side
        refresh behavior.
        """
        if self._closed or self._companion is None or is_deleted(self._companion):
            return False

        self._qt_sync_timer.stop()

        text = self._qt_pending_text
        if text is None:
            text = self._companion.text()

        self._qt_pending_text = None

        ok = self._write_original_text(text, force=True)
        if not ok:
            return False

        self._trigger_extra_actions(text)

        self._baseline_text = text
        self._last_original_text = text
        self._dirty = False

        # If QGIS normalized or transformed the text, reflect that back.
        current = self._read_original_text()
        if current is not None and current != text:
            self._baseline_text = current
            self._last_original_text = current
            self._set_qt_text(current)

        return True

    def _trigger_extra_actions(self, text):
        """
        Trigger additional QGIS refresh behavior.

        This uses only public/introspectable methods. It does not monkey patch
        or subclass QGIS internals.
        """
        self._begin_suppress_original()

        try:
            self._notify_editing_finished()

            if self._kind == "expression":
                expression_widget = find_first_descendant_by_class_substrings(
                    self._dialog,
                    [
                        "qgsexpressionbuilderwidget",
                        "expressionbuilderwidget",
                    ],
                )

                if expression_widget is not None and not is_deleted(expression_widget):
                    setter = getattr(expression_widget, "setExpressionText", None)
                    if callable(setter):
                        try:
                            setter(text)
                        except Exception as e:
                            log_exception(
                                "EditorSyncController._trigger_extra_actions.setExpressionText",
                                e,
                            )

                    validator = getattr(expression_widget, "isExpressionValid", None)
                    if callable(validator):
                        try:
                            validator()
                        except Exception:
                            pass

            elif self._kind == "filter":
                # Some QGIS filter/query implementations expose SQL setters.
                # Call them when present, but do not assume they exist.
                for obj in (self._dialog, self._text_source, self._sci):
                    if obj is None or is_deleted(obj):
                        continue

                    for method_name in ("setSql", "setWhere", "setFilterExpression"):
                        method = getattr(obj, method_name, None)
                        if callable(method):
                            try:
                                method(text)
                            except Exception as e:
                                log_exception(
                                    f"EditorSyncController._trigger_extra_actions.{method_name}",
                                    e,
                                )
                            break

            self._emit_text_changed(self._sci)
            if self._text_source is not self._sci:
                self._emit_text_changed(self._text_source)

        except Exception as e:
            log_exception("EditorSyncController._trigger_extra_actions", e)

    def _notify_editing_finished(self):
        """
        Emit editingFinished-like behavior when possible.

        QScintilla does not have editingFinished(), but some wrapper widgets
        might. We also send synthetic focus-out/focus-in events so widgets
        that validate on focus loss can react.
        """
        target = (
            self._sci
            if self._sci is not None and not is_deleted(self._sci)
            else self._text_source
        )

        if target is None or is_deleted(target):
            return

        signal = getattr(target, "editingFinished", None)
        emitter = getattr(signal, "emit", None)

        if callable(emitter):
            try:
                emitter()
            except Exception:
                pass

        try:
            if isinstance(target, QWidget):
                QCoreApplication.sendEvent(
                    target,
                    QFocusEvent(
                        QEvent.Type.FocusOut,
                        Qt.FocusReason.OtherFocusReason,
                    ),
                )
                QCoreApplication.sendEvent(
                    target,
                    QFocusEvent(
                        QEvent.Type.FocusIn,
                        Qt.FocusReason.OtherFocusReason,
                    ),
                )
        except Exception:
            pass

    def _ok_requested(self):
        """Companion OK button."""
        if self._closed:
            return

        self.apply_to_original()

        if self._companion is not None and not is_deleted(self._companion):
            self._companion.set_close_reason("ok")
            self._companion.close()

    def _cancel_requested(self):
        """Companion Cancel button."""
        if self._closed:
            return

        self._restore_baseline()

        if self._companion is not None and not is_deleted(self._companion):
            self._companion.set_close_reason("cancel")
            self._companion.close()

    def _restore_baseline(self):
        """Restore the baseline text after Cancel."""
        if self._closed or not self._dirty:
            return

        self._qt_sync_timer.stop()
        self._qt_pending_text = None

        if self._baseline_text is None:
            return

        ok = self._write_original_text(self._baseline_text, force=True)
        if ok:
            self._last_original_text = self._baseline_text
            self._dirty = False

    # ------------------------------------------------------------------
    # Companion close handling
    # ------------------------------------------------------------------

    def _companion_closed(self, reason):
        """Companion window closed."""
        if self._closed:
            return

        # Window close button behaves like Cancel.
        if reason not in ("ok", "original", "shutdown"):
            self._restore_baseline()

        self._in_companion_closed = True
        try:
            self.shutdown()
        finally:
            self._in_companion_closed = False

    def _close_for_original(self):
        """Close companion because the original dialog closed."""
        if self._closed:
            return

        if self._companion is not None and not is_deleted(self._companion):
            self._companion.set_close_reason("original")
            self._companion.close()
        else:
            self.shutdown()

    # ------------------------------------------------------------------
    # Synchronization guards
    # ------------------------------------------------------------------

    def _begin_suppress_original(self):
        """Temporarily ignore original-editor change notifications."""
        self._suppress_original_count += 1

        if not self._suppress_original_timer.isActive():
            self._suppress_original_timer.start()

    def _decrement_suppress_original(self):
        """Release one original-editor suppression."""
        if self._suppress_original_count > 0:
            self._suppress_original_count -= 1

        if self._suppress_original_count > 0:
            self._suppress_original_timer.start()

    def _begin_suppress_qt(self):
        """Temporarily ignore companion-editor change notifications."""
        self._suppress_qt_count += 1

        if not self._suppress_qt_timer.isActive():
            self._suppress_qt_timer.start()

    def _decrement_suppress_qt(self):
        """Release one companion-editor suppression."""
        if self._suppress_qt_count > 0:
            self._suppress_qt_count -= 1

        if self._suppress_qt_count > 0:
            self._suppress_qt_timer.start()

    # ------------------------------------------------------------------
    # Shutdown and cleanup
    # ------------------------------------------------------------------

    def shutdown(self):
        """
        Disconnect all signals, remove event filters and delete the companion.

        This must be safe to call multiple times.
        """
        if self._closed:
            return

        self._closed = True

        try:
            self._sync_timer.stop()
        except Exception:
            pass

        try:
            self._qt_sync_timer.stop()
        except Exception:
            pass

        try:
            self._suppress_original_timer.stop()
        except Exception:
            pass

        try:
            self._suppress_qt_timer.stop()
        except Exception:
            pass

        # Disconnect original editor signals.
        for obj, signal in self._original_signal_connections:
            try:
                signal.disconnect(self._on_original_signal)
            except Exception:
                pass

        self._original_signal_connections = []

        # Remove event filters.
        seen = set()
        for obj in (self._dialog, self._sci, self._text_source):
            if obj is None or is_deleted(obj):
                continue

            obj_id = id(obj)
            if obj_id in seen:
                continue
            seen.add(obj_id)

            try:
                obj.removeEventFilter(self)
            except Exception:
                pass

        # Disconnect dialog lifecycle signals.
        if self._dialog is not None and not is_deleted(self._dialog):
            try:
                self._dialog.destroyed.disconnect(self._on_dialog_destroyed)
            except Exception:
                pass

            if isinstance(self._dialog, QDialog):
                try:
                    self._dialog.finished.disconnect(self._on_dialog_finished)
                except Exception:
                    pass

        # Disconnect and delete companion.
        if self._companion is not None and not is_deleted(self._companion):
            try:
                self._companion.editor.textChanged.disconnect(
                    self._on_qt_text_changed
                )
            except Exception:
                pass

            try:
                self._companion.applyRequested.disconnect(self.apply_to_original)
            except Exception:
                pass

            try:
                self._companion.okRequested.disconnect(self._ok_requested)
            except Exception:
                pass

            try:
                self._companion.cancelRequested.disconnect(self._cancel_requested)
            except Exception:
                pass

            try:
                self._companion.closed.disconnect(self._companion_closed)
            except Exception:
                pass

            if not self._in_companion_closed:
                try:
                    if self._companion.isVisible():
                        self._companion.set_close_reason("shutdown")
                        self._companion.close()
                except Exception:
                    pass

            try:
                self._companion.deleteLater()
            except Exception:
                pass

        self._companion = None

        try:
            self.controllerClosed.emit(self)
        except Exception:
            pass