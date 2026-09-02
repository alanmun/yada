"""Application palette.

Qt's default look is whatever the platform hands it, which on Windows means a lot of flat
grey. yada ships a deep-blue palette with white text and a bright blue accent instead, and
keeps "System" available for anyone who would rather match their desktop.

Implemented as a QPalette rather than a stylesheet on purpose. A stylesheet replaces
Qt's native widget drawing wholesale, which means every control needs restyling by hand and
anything missed looks broken. A palette recolours the widgets Qt already draws correctly,
so scrollbars, focus rings and disabled states keep working. It also means HintLabel's
computed colours adapt for free, because they are derived from the live palette.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette

THEMES = ("blue", "system")

THEME_LABELS = {
    "blue": "yada blue",
    "system": "Match my desktop",
}

# Deep navy grounds, near-white text with a faint blue cast so it reads as part of the
# palette rather than pasted on, and one bright accent for selection and focus.
BLUE = {
    "window": "#101c2e",
    "window_text": "#e8f0fb",
    "base": "#0b1522",
    "alternate_base": "#16263c",
    "text": "#e8f0fb",
    "bright_text": "#ffffff",
    "button": "#1b2f4d",
    "button_text": "#e8f0fb",
    "highlight": "#2f81f7",
    "highlighted_text": "#ffffff",
    "tooltip_base": "#1b2f4d",
    "tooltip_text": "#e8f0fb",
    "placeholder": "#7f93ae",
    "link": "#6fb3ff",
    "link_visited": "#a98cff",
    "mid": "#3a5578",
    "dark": "#0a121d",
    "light": "#264268",
    "shadow": "#05090f",
    "disabled_text": "#66799a",
    "disabled_button_text": "#5b6d8c",
}


def blue_palette() -> QPalette:
    palette = QPalette()
    c = {k: QColor(v) for k, v in BLUE.items()}

    for group in (
        QPalette.ColorGroup.Active,
        QPalette.ColorGroup.Inactive,
        QPalette.ColorGroup.Normal,
    ):
        palette.setColor(group, QPalette.ColorRole.Window, c["window"])
        palette.setColor(group, QPalette.ColorRole.WindowText, c["window_text"])
        palette.setColor(group, QPalette.ColorRole.Base, c["base"])
        palette.setColor(group, QPalette.ColorRole.AlternateBase, c["alternate_base"])
        palette.setColor(group, QPalette.ColorRole.Text, c["text"])
        palette.setColor(group, QPalette.ColorRole.BrightText, c["bright_text"])
        palette.setColor(group, QPalette.ColorRole.Button, c["button"])
        palette.setColor(group, QPalette.ColorRole.ButtonText, c["button_text"])
        palette.setColor(group, QPalette.ColorRole.Highlight, c["highlight"])
        palette.setColor(group, QPalette.ColorRole.HighlightedText, c["highlighted_text"])
        palette.setColor(group, QPalette.ColorRole.ToolTipBase, c["tooltip_base"])
        palette.setColor(group, QPalette.ColorRole.ToolTipText, c["tooltip_text"])
        palette.setColor(group, QPalette.ColorRole.PlaceholderText, c["placeholder"])
        palette.setColor(group, QPalette.ColorRole.Link, c["link"])
        palette.setColor(group, QPalette.ColorRole.LinkVisited, c["link_visited"])
        palette.setColor(group, QPalette.ColorRole.Mid, c["mid"])
        palette.setColor(group, QPalette.ColorRole.Dark, c["dark"])
        palette.setColor(group, QPalette.ColorRole.Light, c["light"])
        palette.setColor(group, QPalette.ColorRole.Shadow, c["shadow"])

    # Disabled controls need explicit colours, or Qt derives grey ones that clash.
    disabled = QPalette.ColorGroup.Disabled
    palette.setColor(disabled, QPalette.ColorRole.Window, c["window"])
    palette.setColor(disabled, QPalette.ColorRole.Base, c["base"])
    palette.setColor(disabled, QPalette.ColorRole.Button, c["button"])
    palette.setColor(disabled, QPalette.ColorRole.WindowText, c["disabled_text"])
    palette.setColor(disabled, QPalette.ColorRole.Text, c["disabled_text"])
    palette.setColor(disabled, QPalette.ColorRole.ButtonText, c["disabled_button_text"])
    palette.setColor(disabled, QPalette.ColorRole.Highlight, c["mid"])
    palette.setColor(disabled, QPalette.ColorRole.HighlightedText, c["disabled_text"])
    return palette


# A few things a palette genuinely cannot express: group-box titles, the accent underline
# on the selected tab, and focus rings. Kept deliberately short -- every rule here is a
# widget Qt is no longer drawing natively.
BLUE_STYLESHEET = f"""
QGroupBox {{
    border: 1px solid {BLUE["mid"]};
    border-radius: 6px;
    margin-top: 10px;
    padding-top: 8px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
    color: {BLUE["highlight"]};
    font-weight: 600;
}}
QTabBar::tab:selected {{
    border-bottom: 2px solid {BLUE["highlight"]};
    color: {BLUE["bright_text"]};
}}
QPushButton {{
    border: 1px solid {BLUE["mid"]};
    border-radius: 5px;
    padding: 5px 12px;
}}
QPushButton:hover {{ border-color: {BLUE["highlight"]}; }}
QPushButton:default {{ border-color: {BLUE["highlight"]}; }}
QPushButton:disabled {{ border-color: {BLUE["dark"]}; }}
QLineEdit, QPlainTextEdit, QComboBox, QListWidget, QDoubleSpinBox, QSpinBox {{
    border: 1px solid {BLUE["mid"]};
    border-radius: 5px;
    padding: 3px 6px;
    selection-background-color: {BLUE["highlight"]};
}}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus,
QDoubleSpinBox:focus, QSpinBox:focus {{
    border-color: {BLUE["highlight"]};
}}
QSlider::sub-page:horizontal {{ background: {BLUE["highlight"]}; }}
"""


def apply_theme(app, theme: str) -> None:
    """Apply `theme` to a QApplication. Unknown names fall back to the blue palette."""
    if theme == "system":
        app.setStyleSheet("")
        app.setPalette(app.style().standardPalette())
        return
    app.setPalette(blue_palette())
    app.setStyleSheet(BLUE_STYLESHEET)
