"""Custom widgets: ECAM-style engine gauges and a timeline slider."""
from __future__ import annotations

import math
from typing import Dict, Optional

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QProxyStyle, QSizePolicy, QSlider, QStyle, QWidget

from . import theme

ARC_START = 210.0   # degrees (Qt: 0 = 3 o'clock, counter-clockwise positive)
ARC_SPAN = -240.0


class EngineGauge(QWidget):
    """One engine: primary arc (N1 % or RPM), throttle lever bar, secondary readouts."""

    def __init__(self, engine: int, parent=None) -> None:
        super().__init__(parent)
        self.engine = engine
        self.setMinimumSize(170, 230)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.piston = False
        self.live: Dict[str, float] = {}
        self.rec: Dict[str, float] = {}
        self.active = True

    def set_values(self, live: Dict[str, float], rec: Optional[Dict[str, float]], piston: bool, active: bool) -> None:
        self.live, self.rec, self.piston, self.active = live, rec or {}, piston, active
        self.update()

    # ------------------------------------------------------------------
    def _primary(self, d: Dict[str, float]):
        if self.piston:
            v = d.get(f"eng.rpm.{self.engine}")
            return v, 0.0, 3000.0, "RPM", "{:.0f}"
        v = d.get(f"eng.n1.{self.engine}")
        return v, 0.0, 110.0, "N1 %", "{:.1f}"

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor(theme.PANEL))
        dim = not self.active

        # title
        p.setPen(QColor(theme.MUTED))
        f = QFont(self.font())
        f.setPointSizeF(9)
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRectF(0, 6, w, 18), Qt.AlignHCenter, f"ENG {self.engine}")

        # arc
        size = min(w - 30, h * 0.55)
        cx, cy = w / 2, 28 + size / 2
        r = size / 2
        rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
        live, lo, hi, label, fmt = self._primary(self.live)
        rec, *_ = self._primary(self.rec)

        p.setPen(QPen(QColor(theme.TRACK), 7, Qt.SolidLine, Qt.FlatCap))
        p.drawArc(rect, int(ARC_START * 16), int(ARC_SPAN * 16))
        if live is not None and math.isfinite(live):
            frac = max(0.0, min(1.0, (live - lo) / (hi - lo)))
            col = QColor(theme.MUTED if dim else theme.ECAM_GREEN)
            if not self.piston and live > 100.5:
                col = QColor(theme.AMBER)
            p.setPen(QPen(col, 7, Qt.SolidLine, Qt.FlatCap))
            p.drawArc(rect, int(ARC_START * 16), int(ARC_SPAN * frac * 16))
            # needle
            ang = math.radians(ARC_START + ARC_SPAN * frac)
            p.setPen(QPen(col, 2.5))
            p.drawLine(QPointF(cx, cy), QPointF(cx + math.cos(ang) * (r - 2), cy - math.sin(ang) * (r - 2)))
        if rec is not None and math.isfinite(rec):
            frac = max(0.0, min(1.0, (rec - lo) / (hi - lo)))
            ang = math.radians(ARC_START + ARC_SPAN * frac)
            p.setPen(QPen(QColor(theme.CYAN), 3))
            p.drawLine(QPointF(cx + math.cos(ang) * (r - 12), cy - math.sin(ang) * (r - 12)),
                       QPointF(cx + math.cos(ang) * (r + 8), cy - math.sin(ang) * (r + 8)))

        # value
        f.setPointSizeF(max(11, size / 7.5))
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(theme.MUTED if dim else theme.ECAM_GREEN))
        text = fmt.format(live) if live is not None and math.isfinite(live) else "--"
        p.drawText(QRectF(cx - r, cy + r * 0.05, 2 * r, r * 0.6), Qt.AlignCenter, text)
        f.setPointSizeF(8)
        f.setBold(False)
        p.setFont(f)
        p.setPen(QColor(theme.MUTED))
        p.drawText(QRectF(cx - r, cy + r * 0.55, 2 * r, 16), Qt.AlignCenter, label)

        # throttle lever bar (-100 .. 100 %)
        y = cy + r + 14
        bar = QRectF(16, y, w - 32, 8)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.TRACK))
        p.drawRoundedRect(bar, 4, 4)
        zero_x = bar.left() + bar.width() * (100 / 200)
        lever = self.live.get(f"eng.throttle.{self.engine}")
        if lever is not None and math.isfinite(lever):
            x = bar.left() + bar.width() * (max(-100, min(100, lever)) + 100) / 200
            p.setBrush(QColor(theme.AMBER if lever < 0 else theme.ACCENT))
            p.drawRoundedRect(QRectF(min(zero_x, x), bar.top(), abs(x - zero_x), bar.height()), 4, 4)
        rlever = self.rec.get(f"eng.throttle.{self.engine}")
        if rlever is not None and math.isfinite(rlever):
            x = bar.left() + bar.width() * (max(-100, min(100, rlever)) + 100) / 200
            p.setPen(QPen(QColor(theme.CYAN), 2))
            p.drawLine(QPointF(x, bar.top() - 4), QPointF(x, bar.bottom() + 4))
        p.setPen(QColor(theme.MUTED))
        p.drawText(QRectF(bar.left(), bar.bottom() + 2, bar.width(), 14), Qt.AlignLeft, "REV")
        p.drawText(QRectF(bar.left(), bar.bottom() + 2, bar.width(), 14), Qt.AlignRight,
                   f"THR {lever:.0f}%" if lever is not None and math.isfinite(lever) else "THR --")

        # secondary readouts
        rows = []
        g = self.live.get
        e = self.engine
        if self.piston:
            rows = [("PROP", g(f"eng.prop_rpm.{e}"), "{:.0f}"), ("EGT", g(f"eng.egt.{e}"), "{:.0f}°"),
                    ("FF", g(f"eng.ff_gph.{e}"), "{:.1f} gph")]
        else:
            rows = [("N2", g(f"eng.n2.{e}"), "{:.1f}%"), ("EGT", g(f"eng.egt.{e}"), "{:.0f}°"),
                    ("FF", g(f"eng.ff.{e}"), "{:.0f} pph")]
        rev = g(f"eng.reverser.{e}")
        if rev is not None and rev > 1:
            rows.append(("REV", rev, "{:.0f}%"))
        y = bar.bottom() + 20
        for name, val, vf in rows:
            if y > h - 12:
                break
            p.setPen(QColor(theme.MUTED))
            p.drawText(QRectF(16, y, w / 2, 16), Qt.AlignLeft, name)
            p.setPen(QColor(theme.TEXT if not dim else theme.MUTED))
            txt = vf.format(val) if val is not None and math.isfinite(val) else "--"
            p.drawText(QRectF(w / 2 - 16, y, w / 2, 16), Qt.AlignRight, txt)
            y += 17
        if not dim and self.live.get(f"eng.combustion.{e}", 1.0) < 0.5:
            p.setPen(QColor(theme.AMBER))
            p.drawText(QRectF(0, 6, w - 8, 18), Qt.AlignRight, "OFF")
        p.end()


class _JumpStyle(QProxyStyle):
    """Left click on the groove jumps straight to that position."""

    def styleHint(self, hint, option=None, widget=None, returnData=None):  # noqa: N802
        if hint == QStyle.SH_Slider_AbsoluteSetButtons:
            return int(Qt.LeftButton.value) if hasattr(Qt.LeftButton, "value") else int(Qt.LeftButton)
        return super().styleHint(hint, option, widget, returnData)


class Timeline(QSlider):
    """Slider in milliseconds that tells apart user drags from programmatic updates.

    ``scrubbed`` fires continuously (max ~30x per second) while the handle is
    dragged so the aircraft follows the slider; ``seeked`` fires on release.
    """

    seeked = Signal(float)
    scrubbed = Signal(float)

    def __init__(self, parent=None) -> None:
        super().__init__(Qt.Horizontal, parent)
        # A QProxyStyle built on self.style() would take ownership of the shared
        # application style and delete it on exit (crash) - build on the default instead.
        self._jump_style = _JumpStyle()
        self._jump_style.setParent(self)
        self.setStyle(self._jump_style)
        self.setRange(0, 0)
        self._dragging = False
        self._pending: float | None = None
        self._throttle = QTimer(self)
        self._throttle.setSingleShot(True)
        self._throttle.setInterval(33)
        self._throttle.timeout.connect(self._flush)
        self.sliderPressed.connect(lambda: setattr(self, "_dragging", True))
        self.sliderReleased.connect(self._released)
        self.sliderMoved.connect(self._moved)
        self.actionTriggered.connect(self._action)

    def _moved(self, value: int) -> None:
        self._pending = value / 1000.0
        if not self._throttle.isActive():
            self._flush()
            self._throttle.start()

    def _flush(self) -> None:
        if self._pending is not None:
            t, self._pending = self._pending, None
            self.scrubbed.emit(t)

    def _released(self) -> None:
        self._dragging = False
        self._pending = None
        self._throttle.stop()
        self.seeked.emit(self.value() / 1000.0)

    def _action(self, action) -> None:
        if action in (QSlider.SliderPageStepAdd, QSlider.SliderPageStepSub,
                      QSlider.SliderSingleStepAdd, QSlider.SliderSingleStepSub):
            self.seeked.emit(self.sliderPosition() / 1000.0)

    def set_position(self, t: float, duration: float) -> None:
        if self._dragging:
            return
        self.blockSignals(True)
        self.setRange(0, int(duration * 1000))
        self.setValue(int(t * 1000))
        self.blockSignals(False)


def fmt_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
