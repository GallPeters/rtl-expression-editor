# -*- coding: utf-8 -*-

"""
Shared utility helpers.

These helpers are intentionally defensive: a plugin that watches arbitrary
Qt widgets must never crash QGIS because a widget disappeared or an internal
QGIS class changed slightly.
"""

import traceback

try:
    from PyQt6 import sip
except ImportError:
    sip = None

from PyQt6.QtWidgets import QWidget

from qgis.core import QgsMessageLog, Qgis


PLUGIN_LOG_NAME = "RTL Editor"


try:
    INFO_LEVEL = Qgis.MessageLevel.Info
    WARNING_LEVEL = Qgis.MessageLevel.Warning
    CRITICAL_LEVEL = Qgis.MessageLevel.Critical
except AttributeError:
    # Older QGIS enum style. QGIS 4 should use the scoped enum above.
    INFO_LEVEL = Qgis.Info
    WARNING_LEVEL = Qgis.Warning
    CRITICAL_LEVEL = Qgis.Critical


def is_deleted(obj):
    """
    Return True when the underlying C++ Qt object no longer exists.

    PyQt wrappers can remain alive after Qt deletes the C++ object.
    Calling methods on such wrappers raises RuntimeError.
    """
    if obj is None:
        return True

    if sip is None:
        return False

    try:
        return sip.isdeleted(obj)
    except Exception:
        return False


def log_message(message, level=INFO_LEVEL):
    """Log to the QGIS message log."""
    try:
        QgsMessageLog.logMessage(str(message), PLUGIN_LOG_NAME, level)
    except Exception:
        print(f"[{PLUGIN_LOG_NAME}] {message}")


def log_exception(context, exception):
    """Log an exception with traceback."""
    message = f"{context}: {exception}\n{traceback.format_exc()}"
    log_message(message, CRITICAL_LEVEL)


def class_name(obj):
    """
    Return the Qt meta-object class name.

    This is more stable than Python type names for QGIS internal C++ widgets.
    """
    if is_deleted(obj):
        return ""

    try:
        meta_object = obj.metaObject()
        if meta_object is None:
            return ""
        return meta_object.className() or ""
    except Exception:
        return ""


def is_companion_widget(widget):
    """
    Return True when this widget is one of our companion editor windows.

    This prevents the monitor from attaching a companion to our own companion.
    """
    if is_deleted(widget) or not isinstance(widget, QWidget):
        return False

    try:
        return bool(widget.property("rtl_editor_companion"))
    except Exception:
        return False


def to_unicode(value):
    """Convert Qt/Scintilla text values to Python str safely."""
    if value is None:
        return ""

    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")

    return str(value)


def has_text_methods(obj):
    """
    Return True when the object exposes text() and setText().

    This is used to detect both raw QsciScintilla widgets and QGIS wrapper
    widgets such as QgsCodeEditor subclasses.
    """
    if is_deleted(obj):
        return False

    try:
        return callable(getattr(obj, "text", None)) and callable(
            getattr(obj, "setText", None)
        )
    except Exception:
        return False


def get_widget_text(obj):
    """
    Read text from an object exposing text().

    Returns None when the text cannot be read.
    """
    if is_deleted(obj):
        return None

    try:
        text_method = getattr(obj, "text", None)
        if not callable(text_method):
            return None

        return to_unicode(text_method())
    except Exception:
        return None


def find_descendant_widgets(root):
    """
    Return all descendant QWidget children of root.

    The search is recursive because Qt's findChildren() is recursive.
    """
    if is_deleted(root) or not isinstance(root, QWidget):
        return []

    try:
        children = root.findChildren(QWidget)
    except Exception:
        children = []

    return [child for child in children if not is_deleted(child)]


def find_first_descendant_by_class_substrings(root, substrings):
    """
    Return the first descendant widget whose Qt class name contains one of
    the supplied case-insensitive substrings.
    """
    if is_deleted(root):
        return None

    lowered = [s.lower() for s in substrings]

    for child in find_descendant_widgets(root):
        name = class_name(child).lower()
        if any(part in name for part in lowered):
            return child

    return None


def find_scintilla_child(parent):
    """
    Find the best QScintilla widget below a wrapper widget.

    QGIS code editors usually wrap a QsciScintilla instance inside a
    QgsCodeEditor-derived QWidget. We identify Scintilla by class name so we
    do not need to import QScintilla Python bindings.
    """
    if is_deleted(parent):
        return None

    best = None
    best_score = -1

    for child in find_descendant_widgets(parent):
        name = class_name(child).lower()
        if "scintilla" not in name:
            continue

        if not has_text_methods(child):
            continue

        score = 0

        try:
            if child.isVisible():
                score += 100000
        except Exception:
            pass

        try:
            score += max(0, child.width() * child.height())
        except Exception:
            pass

        if score > best_score:
            best = child
            best_score = score

    return best