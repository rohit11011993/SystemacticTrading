"""Light and dark themes (FR-12.5) and state colours.

State is never carried by colour alone (FR-12.4): every coloured badge also has an icon and a
text label, defined in ``STATE_STYLE``.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

TOKENS = {
    "light": {"bg": "#f6f7f9", "panel": "#ffffff", "text": "#1d2330", "muted": "#5d6678", "border": "#d6dae1",
              "accent": "#2457c5", "ok": "#1a7f37", "warn": "#9a6700", "bad": "#c62828", "neutral": "#5d6678",
              "ok_bg": "#dcf3e3", "warn_bg": "#fff1cc", "bad_bg": "#fde0e0", "neutral_bg": "#eceef2"},
    "dark": {"bg": "#14171c", "panel": "#1c2028", "text": "#e6e9ef", "muted": "#9aa3b2", "border": "#2e3440",
             "accent": "#6c9bff", "ok": "#4cc173", "warn": "#e2b341", "bad": "#ff6b6b", "neutral": "#9aa3b2",
             "ok_bg": "#173823", "warn_bg": "#3a2f12", "bad_bg": "#451c1c", "neutral_bg": "#262b35"},
}

# kind -> (icon, default label). Icons differ in shape, not only colour.
STATE_STYLE = {"ok": "●", "warn": "▲", "bad": "✖", "neutral": "○"}

ALIGNMENT = {"IN_LINE": ("ok", "IN LINE"), "DRIFTING": ("warn", "DRIFTING"), "OFF_THESIS": ("bad", "OFF-THESIS")}
LADDER = {"GREEN": "ok", "YELLOW": "warn", "ORANGE": "warn", "RED": "bad", "BLACK": "bad"}


def apply_theme(app: QApplication, name: str) -> dict[str, str]:
    t = TOKENS.get(name, TOKENS["light"])
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(t["bg"]))
    pal.setColor(QPalette.Base, QColor(t["panel"]))
    pal.setColor(QPalette.AlternateBase, QColor(t["bg"]))
    pal.setColor(QPalette.Text, QColor(t["text"]))
    pal.setColor(QPalette.WindowText, QColor(t["text"]))
    pal.setColor(QPalette.Button, QColor(t["panel"]))
    pal.setColor(QPalette.ButtonText, QColor(t["text"]))
    pal.setColor(QPalette.Highlight, QColor(t["accent"]))
    pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    app.setPalette(pal)
    app.setStyleSheet(f"""
        QWidget {{ color: {t['text']}; font-size: 10pt; }}
        QMainWindow, QStackedWidget {{ background: {t['bg']}; }}
        QFrame#Tile, QGroupBox {{ background: {t['panel']}; border: 1px solid {t['border']}; border-radius: 6px; }}
        QGroupBox {{ margin-top: 14px; padding: 8px; }}
        QGroupBox::title {{ subcontrol-origin: margin; left: 8px; color: {t['muted']}; }}
        QLabel#TileTitle, QLabel#Muted {{ color: {t['muted']}; font-size: 9pt; }}
        QLabel#TileValue {{ font-size: 15pt; font-weight: 600; }}
        QListWidget#Nav {{ background: {t['panel']}; border: none; border-right: 1px solid {t['border']}; }}
        QListWidget#Nav::item {{ padding: 8px 12px; }}
        QListWidget#Nav::item:selected {{ background: {t['accent']}; color: white; }}
        QTableView {{ background: {t['panel']}; gridline-color: {t['border']};
                      selection-background-color: {t['accent']}; }}
        QHeaderView::section {{ background: {t['bg']}; padding: 4px; border: none;
                                border-bottom: 1px solid {t['border']}; font-weight: 600; }}
        QPushButton {{ padding: 5px 12px; border: 1px solid {t['border']}; border-radius: 4px;
                       background: {t['panel']}; }}
        QPushButton:hover {{ border-color: {t['accent']}; }}
        QPushButton#Kill {{ background: {t['bad']}; color: white; font-weight: 700; border: none; }}
        QPushButton#Danger {{ color: {t['bad']}; border-color: {t['bad']}; font-weight: 600; }}
        QFrame#TopBar {{ background: {t['panel']}; border-bottom: 1px solid {t['border']}; }}
        QPlainTextEdit, QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox, QDateEdit {{
            background: {t['panel']}; border: 1px solid {t['border']}; border-radius: 4px; padding: 3px; }}
    """)
    return t


def badge_css(kind: str, theme: dict[str, str]) -> str:
    return (f"QLabel {{ color: {theme[kind]}; background: {theme[kind + '_bg']}; border-radius: 4px;"
            f" padding: 2px 8px; font-weight: 600; }}")
