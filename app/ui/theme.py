"""Dark SaaS-style theme: colour tokens, fonts, global stylesheet and icon helper."""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from app.config import RESOURCES_DIR


class C:
    BG = "#0A0D14"
    SIDEBAR = "#0D111A"
    SURFACE = "#111623"
    CARD = "#141A29"
    CARD_HOVER = "#182036"
    ELEVATED = "#1A2133"
    BORDER = "#222B3F"
    BORDER_SOFT = "#1B2335"
    TEXT = "#E7EAF2"
    TEXT_2 = "#AEB6C8"
    MUTED = "#6F7A90"
    PRIMARY = "#7C6CFF"
    PRIMARY_HOVER = "#8E80FF"
    PRIMARY_SOFT = "rgba(124, 108, 255, 40)"
    ACCENT = "#22D3EE"
    SUCCESS = "#22C55E"
    WARNING = "#F5A524"
    DANGER = "#F05252"
    INFO = "#3B82F6"


AVATAR_PALETTE = ["#7C6CFF", "#22D3EE", "#F472B6", "#F59E0B", "#34D399", "#60A5FA", "#A78BFA", "#FB7185"]

STATUS_COLORS = {
    "Match": C.SUCCESS,
    "New": C.ACCENT,
    "Tracked": C.MUTED,
    "Bot": C.WARNING,
    "Left": C.TEXT_2,
    "Deleted": C.DANGER,
}


def score_color(score: int) -> str:
    if score >= 75:
        return C.SUCCESS
    if score >= 50:
        return C.PRIMARY
    if score >= 25:
        return C.WARNING
    return C.MUTED


def pick_font_family() -> str:
    families = set(QFontDatabase.families())
    for name in ("Inter", "Inter Display", "SF Pro Text", "Segoe UI Variable Text", "Segoe UI",
                 "Helvetica Neue", "Ubuntu", "Cantarell", "Noto Sans", "DejaVu Sans"):
        if name in families:
            return name
    return QFont().defaultFamily()


def app_font() -> QFont:
    font = QFont(pick_font_family())
    font.setPointSizeF(10)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return font


@lru_cache(maxsize=512)
def _svg_pixmap(name: str, color: str, size: int, dpr: float) -> QPixmap:
    path = RESOURCES_DIR / "icons" / f"{name}.svg"
    data = path.read_text(encoding="utf-8").replace("currentColor", color)
    renderer = QSvgRenderer(QByteArray(data.encode("utf-8")))
    px = QPixmap(int(size * dpr), int(size * dpr))
    px.fill(Qt.GlobalColor.transparent)
    painter = QPainter(px)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, size * dpr, size * dpr))
    painter.end()
    px.setDevicePixelRatio(dpr)
    return px


def icon(name: str, color: str = C.TEXT_2, size: int = 18, active_color: str | None = None) -> QIcon:
    from PySide6.QtGui import QGuiApplication

    dpr = QGuiApplication.primaryScreen().devicePixelRatio() if QGuiApplication.primaryScreen() else 1.0
    ic = QIcon()
    ic.addPixmap(_svg_pixmap(name, color, size, dpr), QIcon.Mode.Normal, QIcon.State.Off)
    if active_color:
        ic.addPixmap(_svg_pixmap(name, active_color, size, dpr), QIcon.Mode.Normal, QIcon.State.On)
        ic.addPixmap(_svg_pixmap(name, active_color, size, dpr), QIcon.Mode.Active, QIcon.State.Off)
    return ic


def app_icon() -> QIcon:
    return QIcon(str(RESOURCES_DIR / "icons" / "logo.svg"))


def with_alpha(color: str, alpha: int) -> QColor:
    c = QColor(color)
    c.setAlpha(alpha)
    return c


ICONS_URL = (RESOURCES_DIR / "icons").as_posix()

STYLESHEET = f"""
* {{
    color: {C.TEXT};
    outline: 0;
}}
QMainWindow, QDialog {{
    background: {C.BG};
}}
QWidget#Page, QWidget#PageBody, QScrollArea#PageScroll, QScrollArea#PageScroll > QWidget > QWidget {{
    background: {C.BG};
}}
QToolTip {{
    background: {C.ELEVATED};
    color: {C.TEXT};
    border: 1px solid {C.BORDER};
    padding: 6px 8px;
    border-radius: 6px;
}}

/* ---------- Sidebar ---------- */
QFrame#Sidebar {{
    background: {C.SIDEBAR};
    border-right: 1px solid {C.BORDER_SOFT};
}}
QLabel#BrandName {{ font-size: 15px; font-weight: 700; letter-spacing: 0.2px; }}
QLabel#BrandSub {{ color: {C.MUTED}; font-size: 11px; }}
QLabel#SidebarSection {{
    color: {C.MUTED}; font-size: 10px; font-weight: 700; letter-spacing: 1.2px; padding: 4px 12px;
}}
QPushButton#NavButton {{
    text-align: left;
    padding: 9px 12px;
    border-radius: 9px;
    border: none;
    background: transparent;
    color: {C.TEXT_2};
    font-size: 13px;
    font-weight: 500;
}}
QPushButton#NavButton:hover {{ background: {C.CARD}; color: {C.TEXT}; }}
QPushButton#NavButton:checked {{ background: {C.PRIMARY_SOFT}; color: {C.TEXT}; font-weight: 600; }}
QListWidget#SidebarGroups {{
    background: transparent; border: none;
}}
QListWidget#SidebarGroups::item {{
    padding: 0px; border-radius: 8px; margin: 1px 0px;
}}
QListWidget#SidebarGroups::item:hover {{ background: {C.CARD}; }}
QListWidget#SidebarGroups::item:selected {{ background: {C.PRIMARY_SOFT}; }}
QFrame#AccountCard {{
    background: {C.CARD}; border: 1px solid {C.BORDER_SOFT}; border-radius: 12px;
}}

/* ---------- Typography ---------- */
QLabel#PageTitle {{ font-size: 22px; font-weight: 700; }}
QLabel#PageSubtitle {{ color: {C.MUTED}; font-size: 12px; }}
QLabel#CardTitle {{ font-size: 14px; font-weight: 600; }}
QLabel#CardSubtitle, QLabel#Muted {{ color: {C.MUTED}; font-size: 12px; }}
QLabel#StatValue {{ font-size: 26px; font-weight: 700; }}
QLabel#StatLabel {{ color: {C.TEXT_2}; font-size: 12px; font-weight: 500; }}
QLabel#StatHint {{ color: {C.MUTED}; font-size: 11px; }}
QLabel#FieldLabel {{ color: {C.TEXT_2}; font-size: 12px; font-weight: 600; }}
QLabel#Help {{ color: {C.MUTED}; font-size: 11px; }}
QLabel#SectionLabel {{
    color: {C.MUTED}; font-size: 10px; font-weight: 700; letter-spacing: 1.1px;
}}

/* ---------- Cards ---------- */
QFrame#Card {{
    background: {C.CARD};
    border: 1px solid {C.BORDER_SOFT};
    border-radius: 14px;
}}
QFrame#StatCard {{
    background: {C.CARD};
    border: 1px solid {C.BORDER_SOFT};
    border-radius: 14px;
}}
QFrame#StatCard:hover {{ border-color: {C.BORDER}; background: {C.CARD_HOVER}; }}
QFrame#Divider {{ background: {C.BORDER_SOFT}; max-height: 1px; min-height: 1px; border: none; }}

/* ---------- Buttons ---------- */
QPushButton {{
    background: {C.ELEVATED};
    border: 1px solid {C.BORDER};
    border-radius: 9px;
    padding: 8px 14px;
    font-weight: 600;
    font-size: 12px;
}}
QPushButton:hover {{ background: {C.CARD_HOVER}; border-color: #2E3952; }}
QPushButton:pressed {{ background: {C.SURFACE}; }}
QPushButton:disabled {{ color: {C.MUTED}; background: {C.SURFACE}; border-color: {C.BORDER_SOFT}; }}
QPushButton#Primary {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {C.PRIMARY}, stop:1 #6A5AF0);
    border: 1px solid {C.PRIMARY};
    color: white;
}}
QPushButton#Primary:hover {{ background: {C.PRIMARY_HOVER}; }}
QPushButton#Primary:disabled {{ background: #3A3570; border-color: #3A3570; color: #B8B2F0; }}
QPushButton#Danger {{ background: transparent; border: 1px solid #5B2730; color: #FF8A8A; }}
QPushButton#Danger:hover {{ background: #2A1519; }}
QPushButton#Ghost {{ background: transparent; border: 1px solid transparent; color: {C.TEXT_2}; }}
QPushButton#Ghost:hover {{ background: {C.CARD}; color: {C.TEXT}; }}
QPushButton#Chip {{
    background: {C.SURFACE}; border: 1px solid {C.BORDER}; border-radius: 14px;
    padding: 5px 12px; font-weight: 500; color: {C.TEXT_2};
}}
QPushButton#Chip:hover {{ color: {C.TEXT}; border-color: #34405C; }}
QPushButton#Chip:checked {{ background: {C.PRIMARY_SOFT}; border-color: {C.PRIMARY}; color: {C.TEXT}; }}
QToolButton {{
    background: transparent; border: none; border-radius: 8px; padding: 6px;
}}
QToolButton:hover {{ background: {C.ELEVATED}; }}

/* ---------- Inputs ---------- */
QLineEdit, QSpinBox, QDateEdit, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {C.SURFACE};
    border: 1px solid {C.BORDER};
    border-radius: 9px;
    padding: 7px 10px;
    selection-background-color: {C.PRIMARY};
    font-size: 12px;
}}
QLineEdit:hover, QSpinBox:hover, QDateEdit:hover, QComboBox:hover {{ border-color: #2E3952; }}
QLineEdit:focus, QSpinBox:focus, QDateEdit:focus, QComboBox:focus, QPlainTextEdit:focus {{
    border-color: {C.PRIMARY};
}}
QLineEdit:disabled, QSpinBox:disabled, QDateEdit:disabled, QComboBox:disabled {{ color: {C.MUTED}; }}
QLineEdit#Search {{ padding-left: 34px; min-height: 20px; }}
QLineEdit#CodeInput {{ font-size: 22px; font-weight: 600; letter-spacing: 8px; padding: 10px; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::down-arrow, QDateEdit::down-arrow {{
    image: url({ICONS_URL}/chevron_down.svg); width: 12px; height: 12px; margin-right: 10px;
}}
QComboBox QAbstractItemView {{
    background: {C.ELEVATED}; border: 1px solid {C.BORDER}; border-radius: 8px;
    padding: 4px; selection-background-color: {C.PRIMARY_SOFT}; outline: 0;
}}
QSpinBox::up-button, QSpinBox::down-button, QDateEdit::up-button, QDateEdit::down-button {{
    width: 0; border: none;
}}
QDateEdit::drop-down {{ border: none; width: 22px; }}
QCalendarWidget QWidget {{ background: {C.ELEVATED}; alternate-background-color: {C.SURFACE}; }}
QCalendarWidget QToolButton {{ color: {C.TEXT}; font-weight: 600; }}
QCalendarWidget QAbstractItemView:enabled {{
    color: {C.TEXT}; selection-background-color: {C.PRIMARY}; selection-color: white;
}}
QCheckBox {{ spacing: 8px; color: {C.TEXT_2}; font-size: 12px; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; border-radius: 5px; border: 1px solid #36415A; background: {C.SURFACE};
}}
QCheckBox::indicator:checked {{ background: {C.PRIMARY}; border-color: {C.PRIMARY}; }}
QSlider::groove:horizontal {{ height: 4px; background: {C.BORDER}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {C.PRIMARY}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: white; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px;
}}

/* ---------- Tables ---------- */
QTableView {{
    background: {C.CARD};
    alternate-background-color: #151C2C;
    border: none;
    gridline-color: transparent;
    selection-background-color: #232B47;
    selection-color: {C.TEXT};
    font-size: 12px;
}}
QTableView::item {{ padding: 0px 10px; border-bottom: 1px solid {C.BORDER_SOFT}; }}
QTableView::item:hover {{ background: #1A2236; }}
QTableView::item:selected {{ background: #232B47; }}
QHeaderView {{ background: {C.CARD}; }}
QHeaderView::section {{
    background: {C.CARD};
    color: {C.MUTED};
    border: none;
    border-bottom: 1px solid {C.BORDER};
    padding: 10px 10px;
    font-size: 11px;
    font-weight: 700;
}}
QHeaderView::section:hover {{ color: {C.TEXT_2}; }}
QTableCornerButton::section {{ background: {C.CARD}; border: none; }}

/* ---------- Tabs ---------- */
QTabBar#GroupTabs {{ background: transparent; }}
QTabBar#GroupTabs::tab {{
    background: transparent; color: {C.TEXT_2}; padding: 8px 14px; margin-right: 4px;
    border: none; border-bottom: 2px solid transparent; font-weight: 600; font-size: 12px;
}}
QTabBar#GroupTabs::tab:hover {{ color: {C.TEXT}; }}
QTabBar#GroupTabs::tab:selected {{ color: {C.TEXT}; border-bottom: 2px solid {C.PRIMARY}; }}
QTabBar QToolButton {{ background: {C.ELEVATED}; border-radius: 6px; }}

/* ---------- Scrollbars ---------- */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #2A3349; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #36415C; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #2A3349; border-radius: 4px; min-width: 30px; }}
QScrollBar::handle:horizontal:hover {{ background: #36415C; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---------- Drawer ---------- */
QFrame#Drawer {{
    background: {C.SURFACE};
    border-left: 1px solid {C.BORDER};
}}
QFrame#EvidenceCard {{
    background: {C.CARD}; border: 1px solid {C.BORDER_SOFT}; border-radius: 10px;
}}
QMenu {{
    background: {C.ELEVATED}; border: 1px solid {C.BORDER}; border-radius: 8px; padding: 6px;
}}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: {C.PRIMARY_SOFT}; }}
QMessageBox {{ background: {C.SURFACE}; }}
QFrame#Toast {{
    background: {C.ELEVATED}; border: 1px solid {C.BORDER}; border-radius: 12px;
}}
QFrame#Banner {{
    background: #1B1A33; border: 1px solid #3A3570; border-radius: 12px;
}}
"""
