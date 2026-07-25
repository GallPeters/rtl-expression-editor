# -*- coding: utf-8 -*-

"""
Dialog and editor detection.

This module decides whether a top-level window is a supported QGIS dialog and,
if so, locates the embedded QScintilla-based editor.

This hotfix version is intentionally more aggressive:

- It accepts any QDialog containing a QScintilla/QgsCodeEditor-like widget.
- It falls back to generic detection when QGIS class names change slightly.
- It exposes quick_dialog_hint() so the monitor can cheaply prefilter windows.
"""

from dataclasses import dataclass

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QWidget

from .utils import (
    class_name,
    find_descendant_widgets,
    find_first_descendant_by_class_substrings,
    find_scintilla_child,
    has_text_methods,
    is_companion_widget,
    is_deleted,
)


@dataclass
class Candidate:
    """One possible editor widget found inside a dialog."""

    widget: QWidget
    class_name: str
    text_source: object
    sci: object
    area: int = 0


@dataclass
class EditorTargetInfo:
    """A confirmed dialog/editor pair that should receive a companion editor."""

    dialog: QWidget
    kind: str
    text_source: object
    sci: object
    title: str
    settings_key: str


def quick_dialog_hint(widget):
    """
    Cheap prefilter used by the monitor.

    Returns True when the window title/object/class suggests that this may be
    an expression, filter, query, SQL or builder dialog.
    """
    if is_deleted(widget) or not isinstance(widget, QWidget):
        return False

    cn = class_name(widget).lower()

    try:
        object_name = widget.objectName().lower()
    except Exception:
        object_name = ""

    try:
        title = widget.windowTitle().lower()
    except Exception:
        title = ""

    keywords = (
        "expression",
        "query",
        "filter",
        "sql",
        "builder",
    )

    return any(keyword in cn or keyword in object_name or keyword in title for keyword in keywords)


def _dialog_kind_hint(widget):
    """
    Return a likely dialog kind from dialog-level hints.

    Possible return values:
        "expression"
        "filter"
        None
    """
    cn = class_name(widget).lower()

    try:
        obj = widget.objectName().lower()
    except Exception:
        obj = ""

    try:
        title = widget.windowTitle().lower()
    except Exception:
        title = ""

    # Expression Builder dialog.
    if (
        "expressionbuilderdialog" in cn
        or "expressionbuilder" in cn
        or "expressionbuilder" in obj
        or "expression builder" in title
    ):
        return "expression"

    # Layer filter / Query Builder dialog.
    if (
        "querybuilder" in cn
        or "querybuilder" in obj
        or "query builder" in title
    ):
        return "filter"

    # Generic title-based fallbacks.
    if "expression" in title:
        return "expression"

    if "sql" in title or "query" in title or "filter" in title:
        return "filter"

    return None


def _has_text_changed_signal(obj):
    """Return True when obj has a connectable textChanged signal."""
    if obj is None or is_deleted(obj):
        return False

    signal = getattr(obj, "textChanged", None)
    return hasattr(signal, "connect")


def _find_editor_candidates(dialog):
    """
    Search recursively for possible editor widgets.

    We accept:

    - QsciScintilla-like widgets, detected by class name containing "scintilla"
    - QgsCodeEditor-like wrapper widgets, detected by class prefix "qgscodeeditor"

    For wrapper widgets we also try to locate the underlying QsciScintilla
    child widget.
    """
    candidates = []

    for child in find_descendant_widgets(dialog):
        cn = class_name(child)
        lowered = cn.lower()

        is_scintilla = "scintilla" in lowered
        is_code_editor_wrapper = lowered.startswith("qgscodeeditor")

        if not is_scintilla and not is_code_editor_wrapper:
            continue

        text_source = None
        sci = None

        if is_scintilla:
            if has_text_methods(child):
                text_source = child
                sci = child
        else:
            if has_text_methods(child):
                text_source = child

            sci = find_scintilla_child(child)

            if text_source is None and sci is not None:
                text_source = sci

        if text_source is None:
            continue

        try:
            area = max(0, child.width() * child.height())
        except Exception:
            area = 0

        candidates.append(
            Candidate(
                widget=child,
                class_name=cn,
                text_source=text_source,
                sci=sci,
                area=area,
            )
        )

    return candidates


def _choose_best_candidate(candidates, kind):
    """
    Choose the most likely editor candidate.

    The scoring prefers:

    - known QGIS expression editors for expression dialogs
    - known QGIS SQL editors for filter/query dialogs
    - visible, enabled, focusable widgets
    - large widgets, because the main editor is usually the largest code area
    - widgets with usable textChanged signals
    """
    best = None
    best_score = -1
    best_area = -1

    for candidate in candidates:
        score = 0
        lowered = candidate.class_name.lower()

        if lowered == "qgscodeeditorexpression":
            score += 150 if kind == "expression" else 90
        elif lowered == "qgscodeeditorsql":
            score += 150 if kind == "filter" else 90
        elif lowered.startswith("qgscodeeditor"):
            score += 80
        elif "scintilla" in lowered:
            score += 40

        if kind == "expression" and "expression" in lowered:
            score += 60

        if kind == "filter" and ("sql" in lowered or "query" in lowered):
            score += 60

        widget = candidate.widget

        if not is_deleted(widget):
            try:
                if widget.isVisible():
                    score += 35
            except Exception:
                pass

            try:
                if widget.isEnabled():
                    score += 10
            except Exception:
                pass

            try:
                if widget.focusPolicy() != Qt.FocusPolicy.NoFocus:
                    score += 10
            except Exception:
                pass

            if candidate.area > 100:
                score += 20 + min(50, candidate.area // 5000)

            try:
                object_name = widget.objectName().lower()
            except Exception:
                object_name = ""

            if kind == "expression" and "expression" in object_name:
                score += 20

            if kind == "filter" and (
                "sql" in object_name or "query" in object_name or "filter" in object_name
            ):
                score += 20

            if candidate.text_source is widget:
                score += 15

            if candidate.sci is not None:
                score += 10

            if _has_text_changed_signal(widget) or _has_text_changed_signal(
                candidate.text_source
            ):
                score += 15

        if score > best_score or (score == best_score and candidate.area > best_area):
            best = candidate
            best_score = score
            best_area = candidate.area

    return best


def analyze_dialog(widget):
    """
    Analyze a top-level widget.

    Returns EditorTargetInfo when the widget is a supported dialog containing
    a usable QScintilla-based editor. Otherwise returns None.
    """
    if is_deleted(widget) or not isinstance(widget, QWidget):
        return None

    if is_companion_widget(widget):
        return None

    try:
        if not widget.isWindow():
            return None

        if not widget.isVisible():
            return None
    except Exception:
        return None

    try:
        is_dialog = isinstance(widget, QDialog) or bool(
            widget.windowFlags() & Qt.WindowType.Dialog
        )
    except Exception:
        is_dialog = False

    if not is_dialog:
        # Allow non-QDialog windows only when they strongly look like one of
        # the target dialogs.
        if not quick_dialog_hint(widget):
            return None

    kind = _dialog_kind_hint(widget)
    candidates = _find_editor_candidates(widget)

    if not candidates:
        return None

    # If dialog-level hints did not identify the kind, infer it from children.
    if kind is None:
        expression_widget = find_first_descendant_by_class_substrings(
            widget,
            [
                "qgsexpressionbuilderwidget",
                "expressionbuilderwidget",
            ],
        )

        if expression_widget is not None:
            kind = "expression"
        elif any("expression" in c.class_name.lower() for c in candidates):
            kind = "expression"
        elif any("sql" in c.class_name.lower() for c in candidates):
            kind = "filter"
        elif isinstance(widget, QDialog):
            # Hotfix behavior:
            # Any QDialog containing a QScintilla/QgsCodeEditor-like widget is
            # treated as an expression-like editor. This prevents silent failure
            # if QGIS changes dialog class names or titles.
            kind = "expression"
        else:
            return None

    best = _choose_best_candidate(candidates, kind)

    if best is None:
        return None

    try:
        title = widget.windowTitle()
    except Exception:
        title = ""

    if not title:
        title = "Expression Builder" if kind == "expression" else "Query Builder"

    safe_class = (class_name(widget) or "Dialog").replace("/", "_")
    safe_class = safe_class.replace("\\", "_").replace(":", "_")

    settings_key = f"{kind}/{safe_class}"

    return EditorTargetInfo(
        dialog=widget,
        kind=kind,
        text_source=best.text_source,
        sci=best.sci,
        title=title,
        settings_key=settings_key,
    )