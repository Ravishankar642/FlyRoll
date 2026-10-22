"""Dark cockpit-display theme."""

BG = "#0e1114"
PANEL = "#151a1f"
PANEL_2 = "#1b2128"
BORDER = "#262e37"
TRACK = "#252c34"
TEXT = "#e6e9ec"
MUTED = "#8a96a3"
ACCENT = "#f2a33a"      # amber/orange accent (lever bars, primary buttons)
AMBER = "#ffb020"
ECAM_GREEN = "#3fdc8a"
CYAN = "#46c8ff"        # recorded values ("ghost" markers)
RED = "#ef4b4b"

QSS = f"""
* {{ color: {TEXT}; font-size: 10pt; }}
QMainWindow, QWidget#root {{ background: {BG}; }}
QWidget {{ background: transparent; }}
QFrame#card {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 10px; }}
QLabel#h1 {{ font-size: 15pt; font-weight: 700; letter-spacing: 1px; }}
QLabel#muted, QLabel[muted="true"] {{ color: {MUTED}; }}
QLabel#clock {{ font-family: Consolas, 'DejaVu Sans Mono', monospace; font-size: 13pt; }}
QLabel#pill {{ padding: 3px 10px; border-radius: 10px; background: {PANEL_2}; border: 1px solid {BORDER}; }}
QPushButton {{ background: {PANEL_2}; border: 1px solid {BORDER}; border-radius: 7px; padding: 6px 12px; }}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:disabled {{ color: #55606b; border-color: {PANEL_2}; }}
QPushButton#rec {{ background: #3a1517; border-color: #6e2427; color: #ffd7d7; font-weight: 700; }}
QPushButton#rec:checked {{ background: {RED}; color: white; }}
QPushButton#primary {{ background: {ACCENT}; color: #1b1205; border: none; font-weight: 700; }}
QPushButton#primary:disabled {{ background: #4a3b25; color: #8a7a60; }}
QComboBox, QLineEdit, QDoubleSpinBox, QSpinBox {{ background: {PANEL_2}; border: 1px solid {BORDER};
    border-radius: 6px; padding: 4px 8px; }}
QComboBox QAbstractItemView {{ background: {PANEL_2}; selection-background-color: {BORDER}; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 8px; background: {PANEL}; top: -1px; }}
QTabBar::tab {{ background: transparent; padding: 7px 14px; color: {MUTED}; border: none; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}
QTableWidget, QPlainTextEdit, QTextBrowser {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px;
    gridline-color: {BORDER}; selection-background-color: #2b3440; }}
QHeaderView::section {{ background: {PANEL_2}; color: {MUTED}; border: none; padding: 5px; }}
QSlider::groove:horizontal {{ height: 6px; background: {TRACK}; border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 3px; }}
QSlider::handle:horizontal {{ background: {TEXT}; width: 14px; margin: -5px 0; border-radius: 7px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {BORDER};
    background: {PANEL_2}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QGroupBox {{ border: 1px solid {BORDER}; border-radius: 8px; margin-top: 14px; padding: 10px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; color: {MUTED}; }}
QSplitter::handle {{ background: {BG}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
"""
