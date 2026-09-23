"""Reusable, custom-painted widgets for the dashboard look and feel."""

from __future__ import annotations

from collections import OrderedDict
from datetime import date
from pathlib import Path

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
    QWidgetItem,
)

from app.ui.theme import AVATAR_PALETTE, C, icon, with_alpha

# --------------------------------------------------------------------------- #
# Avatars
# --------------------------------------------------------------------------- #


def initials(name: str) -> str:
    parts = [p for p in name.replace("@", " ").replace("_", " ").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


class AvatarProvider:
    """Renders circular avatar pixmaps (photo or coloured initials) with an LRU cache."""

    def __init__(self, capacity: int = 2000) -> None:
        self._cache: "OrderedDict[tuple, QPixmap]" = OrderedDict()
        self.capacity = capacity

    def get(self, path: str | None, name: str, seed: int, size: int, dpr: float = 1.0) -> QPixmap:
        key = (path, name, seed, size, dpr)
        px = self._cache.get(key)
        if px is not None:
            self._cache.move_to_end(key)
            return px
        px = self._render(path, name, seed, size, dpr)
        self._cache[key] = px
        if len(self._cache) > self.capacity:
            self._cache.popitem(last=False)
        return px

    def invalidate(self) -> None:
        self._cache.clear()

    @staticmethod
    def _render(path: str | None, name: str, seed: int, size: int, dpr: float) -> QPixmap:
        real = int(size * dpr)
        out = QPixmap(real, real)
        out.fill(Qt.GlobalColor.transparent)
        painter = QPainter(out)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        clip = QPainterPath()
        clip.addEllipse(QRectF(0, 0, real, real))
        painter.setClipPath(clip)
        source = QPixmap(path) if path and Path(path).exists() else QPixmap()
        if not source.isNull():
            scaled = source.scaled(real, real, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                   Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap((real - scaled.width()) // 2, (real - scaled.height()) // 2, scaled)
        else:
            base = QColor(AVATAR_PALETTE[abs(seed) % len(AVATAR_PALETTE)])
            grad = QLinearGradient(0, 0, real, real)
            grad.setColorAt(0, base.lighter(115))
            grad.setColorAt(1, base.darker(135))
            painter.fillRect(QRectF(0, 0, real, real), grad)
            font = QFont()
            font.setPixelSize(max(8, int(real * 0.38)))
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor("white"))
            painter.drawText(QRectF(0, 0, real, real), Qt.AlignmentFlag.AlignCenter, initials(name))
        painter.end()
        out.setDevicePixelRatio(dpr)
        return out


AVATARS = AvatarProvider()


class AvatarLabel(QLabel):
    def __init__(self, size: int = 40, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)

    def set_avatar(self, path: str | None, name: str, seed: int) -> None:
        self.setPixmap(AVATARS.get(path, name, seed, self._size, self.devicePixelRatioF()))


# --------------------------------------------------------------------------- #
# Layout helpers
# --------------------------------------------------------------------------- #


def add_shadow(widget: QWidget, blur: int = 28, alpha: int = 90, y: int = 8) -> None:
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, y)
    effect.setColor(QColor(0, 0, 0, alpha))
    widget.setGraphicsEffect(effect)


def divider() -> QFrame:
    line = QFrame()
    line.setObjectName("Divider")
    line.setFrameShape(QFrame.Shape.NoFrame)
    return line


class Card(QFrame):
    """Rounded card with optional title row."""

    def __init__(self, title: str = "", subtitle: str = "", parent: QWidget | None = None,
                 margins: int = 18) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.outer = QVBoxLayout(self)
        self.outer.setContentsMargins(margins, margins, margins, margins)
        self.outer.setSpacing(12)
        self.header = QHBoxLayout()
        self.header.setSpacing(8)
        if title:
            titles = QVBoxLayout()
            titles.setSpacing(2)
            t = QLabel(title)
            t.setObjectName("CardTitle")
            titles.addWidget(t)
            if subtitle:
                s = QLabel(subtitle)
                s.setObjectName("CardSubtitle")
                s.setWordWrap(True)
                titles.addWidget(s)
            self.header.addLayout(titles, 1)
            self.outer.addLayout(self.header)
        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        self.outer.addLayout(self.body, 1)


class PageHeader(QWidget):
    def __init__(self, title: str, subtitle: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        titles = QVBoxLayout()
        titles.setSpacing(3)
        t = QLabel(title)
        t.setObjectName("PageTitle")
        self.subtitle = QLabel(subtitle)
        self.subtitle.setObjectName("PageSubtitle")
        titles.addWidget(t)
        titles.addWidget(self.subtitle)
        row.addLayout(titles, 1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        row.addLayout(self.actions)

    def add_action(self, widget: QWidget) -> None:
        self.actions.addWidget(widget)


def make_button(text: str, icon_name: str | None = None, kind: str = "", tooltip: str = "") -> QPushButton:
    btn = QPushButton(text)
    if kind:
        btn.setObjectName(kind)
    if icon_name:
        color = "#FFFFFF" if kind == "Primary" else ("#FF8A8A" if kind == "Danger" else C.TEXT_2)
        btn.setIcon(icon(icon_name, color, 16))
        btn.setIconSize(QSize(16, 16))
    if tooltip:
        btn.setToolTip(tooltip)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    return btn


class FlowLayout(QLayout):
    """Wraps child widgets onto new lines when the width is too small."""

    def __init__(self, parent: QWidget | None = None, spacing: int = 8) -> None:
        super().__init__(parent)
        self._items: list = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:  # noqa: N802 (Qt API)
        self._items.append(item)

    def addWidget(self, widget: QWidget) -> None:  # noqa: N802
        self.addChildWidget(widget)
        self.addItem(QWidgetItem(widget))

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), dry=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, dry=False)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do_layout(self, rect: QRect, dry: bool) -> int:
        x, y, line_h = rect.x(), rect.y(), 0
        for item in self._items:
            if item.widget() is not None and item.widget().isHidden():
                continue
            hint = item.sizeHint()
            next_x = x + hint.width() + self._spacing
            if next_x - self._spacing > rect.right() and line_h > 0:
                x = rect.x()
                y += line_h + self._spacing
                next_x = x + hint.width() + self._spacing
                line_h = 0
            if not dry:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y()


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #


class SearchBox(QLineEdit):
    def __init__(self, placeholder: str = "Search…", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Search")
        self.setPlaceholderText(placeholder)
        self.setClearButtonEnabled(True)
        self._icon = icon("search", C.MUTED, 16).pixmap(16, 16)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        painter = QPainter(self)
        painter.drawPixmap(12, (self.height() - 16) // 2, self._icon)
        painter.end()


class ToggleSwitch(QAbstractButton):
    """An animated iOS-style switch."""

    def __init__(self, checked: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(40, 22)
        self._pos = 1.0 if checked else 0.0
        self._anim = QPropertyAnimation(self, b"offset", self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)

    def _animate(self, checked: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def _get_offset(self) -> float:
        return self._pos

    def _set_offset(self, value: float) -> None:
        self._pos = value
        self.update()

    offset = Property(float, _get_offset, _set_offset)

    def setChecked(self, checked: bool) -> None:  # noqa: N802
        super().setChecked(checked)
        self._pos = 1.0 if checked else 0.0
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(40, 22)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        track_off = QColor("#2A3349")
        track_on = QColor(C.PRIMARY)
        t = self._pos
        track = QColor(
            int(track_off.red() + (track_on.red() - track_off.red()) * t),
            int(track_off.green() + (track_on.green() - track_off.green()) * t),
            int(track_off.blue() + (track_on.blue() - track_off.blue()) * t),
        )
        if not self.isEnabled():
            track.setAlpha(110)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0, 0, self.width(), self.height()), 11, 11)
        d = self.height() - 6
        x = 3 + (self.width() - d - 6) * t
        p.setBrush(QColor("white"))
        p.drawEllipse(QRectF(x, 3, d, d))
        p.end()


class ToggleRow(QWidget):
    """Label + description on the left, switch on the right."""

    toggled = Signal(bool)

    def __init__(self, title: str, description: str = "", checked: bool = False,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 2, 0, 2)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        t = QLabel(title)
        t.setObjectName("FieldLabel")
        texts.addWidget(t)
        if description:
            d = QLabel(description)
            d.setObjectName("Help")
            d.setWordWrap(True)
            texts.addWidget(d)
        row.addLayout(texts, 1)
        self.switch = ToggleSwitch(checked)
        self.switch.toggled.connect(self.toggled)
        row.addWidget(self.switch, 0, Qt.AlignmentFlag.AlignVCenter)

    def isChecked(self) -> bool:  # noqa: N802
        return self.switch.isChecked()

    def setChecked(self, value: bool) -> None:  # noqa: N802
        self.switch.setChecked(value)


# --------------------------------------------------------------------------- #
# Status / stats
# --------------------------------------------------------------------------- #


class PulseDot(QWidget):
    """A status dot that softly pulses while active."""

    def __init__(self, color: str = C.SUCCESS, size: int = 10, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = QColor(color)
        self._phase = 0.0
        self._size = size
        self.setFixedSize(size * 2 + 2, size * 2 + 2)
        self._anim = QVariantAnimation(self, startValue=0.0, endValue=1.0, duration=1400, loopCount=-1)
        self._anim.valueChanged.connect(self._tick)

    def _tick(self, value) -> None:
        self._phase = float(value)
        self.update()

    def set_color(self, color: str, pulsing: bool) -> None:
        self._color = QColor(color)
        if pulsing and self._anim.state() != QVariantAnimation.State.Running:
            self._anim.start()
        elif not pulsing:
            self._anim.stop()
            self._phase = 0.0
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = QPointF(self.width() / 2, self.height() / 2)
        r = self._size / 2
        if self._anim.state() == QVariantAnimation.State.Running:
            halo = QColor(self._color)
            halo.setAlphaF(0.45 * (1 - self._phase))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(halo)
            rr = r + r * 1.1 * self._phase
            p.drawEllipse(center, rr, rr)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._color)
        p.drawEllipse(center, r, r)
        p.end()


class StatCard(QFrame):
    clicked = Signal()

    def __init__(self, label: str, icon_name: str, accent: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("StatCard")
        self.setMinimumHeight(118)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._accent = accent
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(6)
        top = QHBoxLayout()
        self.label = QLabel(label)
        self.label.setObjectName("StatLabel")
        top.addWidget(self.label, 1)
        badge = QLabel()
        badge.setFixedSize(32, 32)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setPixmap(icon(icon_name, accent, 17).pixmap(17, 17))
        badge.setStyleSheet(
            f"background: rgba({QColor(accent).red()}, {QColor(accent).green()}, {QColor(accent).blue()}, 34);"
            "border-radius: 9px;"
        )
        top.addWidget(badge)
        layout.addLayout(top)
        value_row = QHBoxLayout()
        value_row.setSpacing(8)
        self.value = QLabel("0")
        self.value.setObjectName("StatValue")
        value_row.addWidget(self.value)
        self.extra = QWidget()
        self.extra_layout = QHBoxLayout(self.extra)
        self.extra_layout.setContentsMargins(0, 0, 0, 0)
        value_row.addWidget(self.extra)
        value_row.addStretch(1)
        layout.addLayout(value_row)
        self.hint = QLabel("")
        self.hint.setObjectName("StatHint")
        layout.addWidget(self.hint)
        self._current = 0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(650)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(lambda v: self.value.setText(f"{int(v):,}"))
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_value(self, value: int, hint: str = "") -> None:
        self.hint.setText(hint)
        if value == self._current:
            self.value.setText(f"{value:,}")
            return
        self._anim.stop()
        self._anim.setStartValue(self._current)
        self._anim.setEndValue(value)
        self._current = value
        self._anim.start()

    def set_text(self, text: str, hint: str = "") -> None:
        self._anim.stop()
        self.value.setText(text)
        self.hint.setText(hint)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class Badge(QLabel):
    def __init__(self, text: str = "", color: str = C.MUTED, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.set_badge(text, color)

    def set_badge(self, text: str, color: str) -> None:
        c = QColor(color)
        self.setText(text)
        self.setStyleSheet(
            f"background: rgba({c.red()},{c.green()},{c.blue()},38); color: {color};"
            "border-radius: 9px; padding: 3px 9px; font-size: 11px; font-weight: 600;"
        )


class BarChart(QWidget):
    """Minimal bar chart (detections per day) painted with QPainter."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._data: list[tuple[date, int]] = []
        self._progress = 1.0
        self._hover = -1
        self.setMinimumHeight(190)
        self.setMouseTracking(True)
        self._anim = QVariantAnimation(self, startValue=0.0, endValue=1.0, duration=700)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._set_progress)

    def _set_progress(self, v) -> None:
        self._progress = float(v)
        self.update()

    def set_data(self, data: list[tuple[date, int]]) -> None:
        changed = [v for _, v in data] != [v for _, v in self._data]
        self._data = data
        if changed:
            self._anim.stop()
            self._anim.start()
        self.update()

    def _bars(self) -> list[QRectF]:
        if not self._data:
            return []
        left, right, top, bottom = 34, 8, 14, 26
        w = self.width() - left - right
        h = self.height() - top - bottom
        n = len(self._data)
        slot = w / n
        bw = max(4.0, min(28.0, slot * 0.58))
        peak = max(1, max(v for _, v in self._data))
        rects = []
        for i, (_, v) in enumerate(self._data):
            bh = h * (v / peak) * self._progress
            x = left + slot * i + (slot - bw) / 2
            rects.append(QRectF(x, top + h - bh, bw, max(bh, 2)))
        return rects

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        idx = -1
        for i, r in enumerate(self._bars()):
            if r.left() - 6 <= event.position().x() <= r.right() + 6:
                idx = i
        if idx != self._hover:
            self._hover = idx
            self.update()

    def leaveEvent(self, _event) -> None:  # noqa: N802
        self._hover = -1
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        left, top, bottom = 34, 14, 26
        h = self.height() - top - bottom
        peak = max([1] + [v for _, v in self._data])
        small = QFont(self.font())
        small.setPixelSize(10)
        p.setFont(small)
        for frac in (0, 0.5, 1.0):
            y = top + h - h * frac
            p.setPen(QPen(QColor(C.BORDER_SOFT), 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(left, y), QPointF(self.width() - 8, y))
            p.setPen(QColor(C.MUTED))
            p.drawText(QRectF(0, y - 7, left - 8, 14), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       f"{int(peak * frac)}")
        bars = self._bars()
        for i, (rect, (day, value)) in enumerate(zip(bars, self._data)):
            grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
            top_c = QColor(C.ACCENT if i == self._hover else C.PRIMARY)
            grad.setColorAt(0, top_c)
            grad.setColorAt(1, with_alpha(C.PRIMARY, 70))
            path = QPainterPath()
            path.addRoundedRect(rect, 4, 4)
            p.fillPath(path, QBrush(grad))
            if i % 2 == 0 or len(self._data) <= 7 or i == len(bars) - 1:
                p.setPen(QColor(C.MUTED))
                p.drawText(QRectF(rect.center().x() - 24, self.height() - bottom + 6, 48, 14),
                           Qt.AlignmentFlag.AlignCenter, day.strftime("%d %b"))
            if i == self._hover:
                label = f"{value:,} new · {day.strftime('%a %d %b')}"
                fm = p.fontMetrics()
                tw = fm.horizontalAdvance(label) + 16
                tx = min(max(rect.center().x() - tw / 2, 0), self.width() - tw)
                tip = QRectF(tx, max(0, rect.top() - 28), tw, 22)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(C.ELEVATED))
                p.drawRoundedRect(tip, 6, 6)
                p.setPen(QColor(C.TEXT))
                p.drawText(tip, Qt.AlignmentFlag.AlignCenter, label)
        p.end()


class EmptyState(QWidget):
    def __init__(self, icon_name: str, title: str, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(8)
        ic = QLabel()
        ic.setPixmap(icon(icon_name, C.MUTED, 36).pixmap(36, 36))
        ic.setAlignment(Qt.AlignmentFlag.AlignCenter)
        t = QLabel(title)
        t.setObjectName("CardTitle")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text = QLabel(text)
        self.text.setObjectName("Muted")
        self.text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text.setWordWrap(True)
        self.text.setMaximumWidth(420)
        layout.addWidget(ic)
        layout.addWidget(t)
        layout.addWidget(self.text, 0, Qt.AlignmentFlag.AlignHCenter)


class Toast(QFrame):
    """Transient in-app notification that fades in at the bottom-right corner."""

    def __init__(self, parent: QWidget, title: str, body: str, accent: str = C.PRIMARY,
                 duration_ms: int = 5000) -> None:
        super().__init__(parent)
        self.setObjectName("Toast")
        self.setFixedWidth(340)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(12)
        bar = QFrame()
        bar.setFixedWidth(3)
        bar.setStyleSheet(f"background: {accent}; border-radius: 1px;")
        layout.addWidget(bar)
        texts = QVBoxLayout()
        texts.setSpacing(3)
        t = QLabel(title)
        t.setObjectName("CardTitle")
        b = QLabel(body)
        b.setObjectName("Muted")
        b.setWordWrap(True)
        texts.addWidget(t)
        texts.addWidget(b)
        layout.addLayout(texts, 1)
        self.adjustSize()
        self._opacity = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity)
        self._fade = QPropertyAnimation(self._opacity, b"opacity", self)
        self._fade.setDuration(220)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        QTimer.singleShot(duration_ms, self.dismiss)

    def show_at(self, bottom_offset: int) -> None:
        parent = self.parentWidget()
        self.adjustSize()
        self.move(parent.width() - self.width() - 24, parent.height() - self.height() - 24 - bottom_offset)
        self.show()
        self.raise_()
        self._fade.start()

    def dismiss(self) -> None:
        self._fade.stop()
        self._fade.setStartValue(self._opacity.opacity())
        self._fade.setEndValue(0.0)
        self._fade.finished.connect(self.deleteLater)
        self._fade.start()

    def mousePressEvent(self, _event) -> None:  # noqa: N802
        self.dismiss()
