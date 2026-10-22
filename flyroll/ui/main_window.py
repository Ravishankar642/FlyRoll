"""FlyRoll main window."""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QKeySequence, QPixmap, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser,
    QVBoxLayout, QWidget,
)

from .. import APP_NAME, __version__
from ..core.channels import ENGINE_TYPE_PISTON, LIGHT_DEFS, MAX_ENGINES
from ..core.flight import Flight, Sampler
from ..core.logbook import Logbook
from ..core.profiles import LVarSpec, Profile, ProfileStore
from ..core.session import DISCONNECTED, IDLE, RECORDING, REPLAY, ReplayOptions, Session, SessionListener
from . import theme
from .update_ui import UpdateController
from .widgets import EngineGauge, Timeline, fmt_time


class Bridge(QObject, SessionListener):
    """Turns session callbacks (worker thread) into Qt signals (UI thread)."""

    state = Signal(str)
    message = Signal(str, str)
    aircraft = Signal(object)
    live = Signal(object)
    recorded = Signal(object)
    position = Signal(float, float, bool)
    probe = Signal(object)

    def on_state(self, state): self.state.emit(state)
    def on_message(self, text, level="info"): self.message.emit(text, level)
    def on_aircraft(self, info): self.aircraft.emit(info)
    def on_live(self, values): self.live.emit(values)
    def on_recorded(self, flight): self.recorded.emit(flight)
    def on_replay_position(self, t, duration, playing): self.position.emit(t, duration, playing)
    def on_probe(self, values): self.probe.emit(values)


def card(*widgets, layout=None) -> QFrame:
    f = QFrame()
    f.setObjectName("card")
    lay = layout or QVBoxLayout()
    lay.setContentsMargins(12, 10, 12, 10)
    f.setLayout(lay)
    for w in widgets:
        lay.addWidget(w)
    return f


def muted(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setProperty("muted", True)
    lbl.setWordWrap(True)
    return lbl


def app_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(theme.ACCENT))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(4, 4, 56, 56, 14, 14)
    p.setPen(QColor("#1b1205"))
    f = QFont()
    f.setBold(True)
    f.setPointSize(26)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, "F")
    p.end()
    return QIcon(pm)


THROTTLE_CHOICES = [("profile", "Use aircraft profile"), ("auto", "Auto"),
                    ("lvar", "Aircraft thrust-lever LVars"), ("axis", "Recorded hardware throttle axis"),
                    ("simvar", "Throttle simvar (Sky Dolly method)")]
SPOOL_CHOICES = [("profile", "Use aircraft profile"), ("assist", "Assist - correct drift"),
                 ("force", "Force - every frame"), ("off", "Off - engines follow levers")]
START_CHOICES = [("profile", "Use aircraft profile"), ("on", "Auto start / shutdown"), ("off", "Never")]


class MainWindow(QMainWindow):
    def __init__(self, session: Session, logbook: Logbook, profiles: ProfileStore, bridge: Bridge) -> None:
        super().__init__()
        self.session, self.logbook, self.profiles, self.bridge = session, logbook, profiles, bridge
        self.flight: Optional[Flight] = None
        self.sampler: Optional[Sampler] = None
        self.state = DISCONNECTED
        self.replay_t = 0.0
        self.replay_playing = False
        self.live: Dict[str, float] = {}
        self.aircraft_info: dict = {}
        self.probe_values: Dict[str, float] = {}
        self._replay_starting = False

        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.setWindowIcon(app_icon())
        self.resize(1280, 800)
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(14, 12, 14, 10)
        outer.setSpacing(10)

        outer.addLayout(self._build_header())
        outer.addWidget(self._build_transport())
        split = QSplitter(Qt.Horizontal)
        split.addWidget(self._build_logbook())
        split.addWidget(self._build_tabs())
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 6)
        split.setSizes([480, 780])
        outer.addWidget(split, 1)
        self.status = QLabel("")
        self.status.setProperty("muted", True)
        outer.addWidget(self.status)

        b = bridge
        b.state.connect(self._on_state)
        b.message.connect(self._on_message)
        b.aircraft.connect(self._on_aircraft)
        b.live.connect(self._on_live)
        b.recorded.connect(self._on_recorded)
        b.position.connect(self._on_position)
        b.probe.connect(self._on_probe)

        self._shortcuts()
        self.updates.start()
        self._refresh_logbook()
        self._on_state(DISCONNECTED)
        self._apply_options()

    # ================================================================== layout
    def _build_header(self) -> QHBoxLayout:
        h = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(30, 30))
        h.addWidget(logo)
        title = QLabel(APP_NAME)
        title.setObjectName("h1")
        h.addWidget(title)
        h.addWidget(muted(f"v{__version__}  ·  record & replay for MSFS 2020 / 2024"))
        h.addStretch(1)
        self.aircraft_lbl = QLabel("No aircraft")
        h.addWidget(self.aircraft_lbl)
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(240)
        self.profile_combo.currentIndexChanged.connect(self._on_profile_combo)
        h.addWidget(self.profile_combo)
        self.conn_lbl = QLabel("● Disconnected")
        self.conn_lbl.setObjectName("pill")
        h.addWidget(self.conn_lbl)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self._toggle_connect)
        h.addWidget(self.connect_btn)
        self.updates = UpdateController(self, lambda: self.state == RECORDING,
                                        lambda text, level: self._on_message(text, level))
        h.insertWidget(h.indexOf(self.aircraft_lbl), self.updates.button)
        h.addWidget(self.updates.menu_button)
        self._fill_profile_combo()
        return h

    def _build_transport(self) -> QFrame:
        lay = QHBoxLayout()
        lay.setSpacing(8)
        self.rec_btn = QPushButton("●  REC")
        self.rec_btn.setObjectName("rec")
        self.rec_btn.setCheckable(True)
        self.rec_btn.setToolTip("Start / stop recording (Ctrl+R)")
        self.rec_btn.clicked.connect(self._toggle_record)
        self.play_btn = QPushButton("▶  Play")
        self.play_btn.setObjectName("primary")
        self.play_btn.setMinimumWidth(96)
        self.play_btn.setToolTip("Play / pause (Space)")
        self.play_btn.clicked.connect(self._toggle_play)
        self.stop_btn = QPushButton("■  Stop")
        self.stop_btn.clicked.connect(self._stop)
        self.begin_btn = QPushButton("⏮")
        self.begin_btn.clicked.connect(lambda: self._seek(0.0))
        self.clock_lbl = QLabel("00:00:00 / 00:00:00")
        self.clock_lbl.setObjectName("clock")
        self.timeline = Timeline()
        self.timeline.seeked.connect(self._seek)
        self.timeline.scrubbed.connect(self._scrub)
        self.speed = QComboBox()
        for s in (0.25, 0.5, 1.0, 2.0, 4.0, 8.0):
            self.speed.addItem(f"{s:g}×", s)
        self.speed.setCurrentIndex(2)
        self.speed.currentIndexChanged.connect(lambda: self.session.set_speed(self.speed.currentData()))
        self.loop = QCheckBox("Loop")
        self.loop.toggled.connect(self._apply_options)
        self.flight_lbl = muted("No flight loaded")
        for w in (self.rec_btn, self.play_btn, self.stop_btn, self.begin_btn, self.clock_lbl):
            lay.addWidget(w)
        lay.addWidget(self.timeline, 1)
        for w in (self.speed, self.loop):
            lay.addWidget(w)
        wrap = QVBoxLayout()
        wrap.addLayout(lay)
        wrap.addWidget(self.flight_lbl)
        return card(layout=wrap)

    def _build_logbook(self) -> QFrame:
        head = QHBoxLayout()
        t = QLabel("Logbook")
        t.setStyleSheet("font-weight:700; font-size:11pt;")
        head.addWidget(t)
        head.addStretch(1)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Date", "Title", "Profile", "Duration", "Samples"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        for c in (0, 2, 3, 4):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._on_select_flight)
        self.table.cellDoubleClicked.connect(lambda *_: self._play_selected())
        btns = QHBoxLayout()
        for text, fn in (("Rename", self._rename), ("Delete", self._delete), ("Import…", self._import),
                         ("Export…", self._export), ("CSV…", self._export_csv)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            btns.addWidget(b)
        lay = QVBoxLayout()
        lay.addLayout(head)
        lay.addWidget(self.table, 1)
        lay.addLayout(btns)
        return card(layout=lay)

    def _build_tabs(self) -> QTabWidget:
        tabs = QTabWidget()
        tabs.addTab(self._build_engines_tab(), "Engines")
        tabs.addTab(self._build_flight_tab(), "Flight")
        tabs.addTab(self._build_replay_tab(), "Replay settings")
        tabs.addTab(self._build_lvar_tab(), "Profiles && LVars")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        tabs.addTab(self.log, "Log")
        return tabs

    def _build_engines_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.engine_hint = muted("Live values from the simulator. During replay the cyan marks show the recording.")
        lay.addWidget(self.engine_hint)
        grid = QHBoxLayout()
        self.gauges = [EngineGauge(i) for i in range(1, MAX_ENGINES + 1)]
        for g in self.gauges:
            grid.addWidget(g)
        lay.addLayout(grid, 1)
        self.plan_lbl = muted("")
        lay.addWidget(self.plan_lbl)
        return w

    def _build_flight_tab(self) -> QWidget:
        w = QWidget()
        grid = QGridLayout(w)
        self.flight_vals: Dict[str, QLabel] = {}
        items = [("info.ias", "IAS", "{:.0f} kt"), ("info.gs", "Ground speed", "{:.0f} kt"),
                 ("pos.alt", "Altitude", "{:,.0f} ft"), ("info.agl", "Above ground", "{:,.0f} ft"),
                 ("info.vs", "Vertical speed", "{:+,.0f} fpm"), ("att.hdg", "Heading", "{:03.0f}°"),
                 ("att.pitch", "Pitch", "{:+.1f}°"), ("att.bank", "Bank", "{:+.1f}°"),
                 ("sec.flaps_index", "Flaps handle", "{:.0f}"), ("sec.spoilers", "Spoilers", "{:.0f}%"),
                 ("hdl.gear", "Gear handle", "bool:DOWN/UP"), ("hdl.parking_brake", "Parking brake", "bool:SET/OFF")]
        self._flight_fmt = {k: f for k, _, f in items}
        for n, (key, label, _) in enumerate(items):
            r, c = divmod(n, 2)
            grid.addWidget(muted(label), r, c * 2)
            v = QLabel("--")
            v.setStyleSheet("font-size: 12pt; font-weight: 600;")
            grid.addWidget(v, r, c * 2 + 1)
            self.flight_vals[key] = v
        row = len(items) // 2 + 1
        grid.addWidget(muted("Lights"), row, 0)
        lights = QHBoxLayout()
        self.light_lbls: Dict[str, QLabel] = {}
        for key, simvar, _ in LIGHT_DEFS:
            l = QLabel(simvar.replace("LIGHT ", "").title())
            l.setObjectName("pill")
            lights.addWidget(l)
            self.light_lbls[key] = l
        lights.addStretch(1)
        grid.addLayout(lights, row, 1, 1, 3)
        grid.setRowStretch(row + 1, 1)
        return w

    def _build_replay_tab(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        what = QGroupBox("What to replay")
        wl = QVBoxLayout(what)
        self.opt: Dict[str, QCheckBox] = {}
        for key, label in (("position", "Position && attitude (freezes your aircraft)"),
                           ("controls", "Flight controls (aileron, elevator, rudder)"),
                           ("flaps", "Flaps"), ("spoilers", "Spoilers"), ("gear", "Gear && parking brake"),
                           ("lights", "Lights"), ("engines", "Engines (levers, N1/N2/RPM, LVars)")):
            cb = QCheckBox(label)
            cb.setChecked(True)
            cb.toggled.connect(self._apply_options)
            wl.addWidget(cb)
            self.opt[key] = cb
        wl.addStretch(1)
        eng = QGroupBox("Engines")
        form = QFormLayout(eng)
        self.throttle_combo = QComboBox()
        for k, label in THROTTLE_CHOICES:
            self.throttle_combo.addItem(label, k)
        self.spool_combo = QComboBox()
        for k, label in SPOOL_CHOICES:
            self.spool_combo.addItem(label, k)
        self.start_combo = QComboBox()
        for k, label in START_CHOICES:
            self.start_combo.addItem(label, k)
        for c in (self.throttle_combo, self.spool_combo, self.start_combo):
            c.currentIndexChanged.connect(self._apply_options)
        self.rate_spin = QDoubleSpinBox()
        self.rate_spin.setRange(5, 120)
        self.rate_spin.setValue(self.session.sample_rate)
        self.rate_spin.setSuffix(" Hz")
        self.rate_spin.valueChanged.connect(lambda v: setattr(self.session, "sample_rate", float(v)))
        form.addRow("Thrust levers", self.throttle_combo)
        form.addRow("N1 / N2 / RPM", self.spool_combo)
        form.addRow("Engine start", self.start_combo)
        self.vel_combo = QComboBox()
        for k, label in (("smoothed", "Smoothed (recommended)"), ("raw", "Raw recorded"), ("off", "Off")):
            self.vel_combo.addItem(label, k)
        self.rot_check = QCheckBox("Also write rotation rates")
        self.flex_combo = QComboBox()
        for k, label in (("recorded", "Recorded wing flex"), ("physics", "Flight model physics")):
            self.flex_combo.addItem(label, k)
        for c in (self.vel_combo, self.flex_combo):
            c.currentIndexChanged.connect(self._apply_options)
        self.rot_check.toggled.connect(self._apply_options)
        form.addRow("Body velocities", self.vel_combo)
        form.addRow("", self.rot_check)
        form.addRow("Wing flex", self.flex_combo)
        form.addRow("Recording rate", self.rate_spin)
        help_ = muted(
            "• Thrust-lever LVars drive the aircraft's own levers (Fenix).\n"
            "• Hardware throttle axis re-sends your recorded quadrant input (iniBuilds, FSLabs, "
            "FBW, PMDG, iFly) - don't move the throttle during replay.\n"
            "• N1/N2 assist keeps the engines matched on autothrust segments.\n"
            "• Fly-by-wire profiles don't replay stick input (it would deflect ailerons and "
            "roll spoilers fully while the replay holds the aircraft).\n"
            "• Start replays on study-level aircraft with the engines running.")
        wl.insertSpacing(wl.count() - 1, 10)
        wl.insertWidget(wl.count() - 1, help_)
        lay.addWidget(what, 1)
        lay.addWidget(eng, 1)
        return w

    def _build_lvar_tab(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        left = QVBoxLayout()
        self.profile_view = QTextBrowser()
        left.addWidget(QLabel("Active profile"))
        left.addWidget(self.profile_view, 1)
        open_dir = QPushButton("Open user profile folder")
        open_dir.clicked.connect(self._open_profile_dir)
        reload_ = QPushButton("Reload profiles")
        reload_.clicked.connect(self._reload_profiles)
        row = QHBoxLayout()
        row.addWidget(open_dir)
        row.addWidget(reload_)
        left.addLayout(row)
        right = QVBoxLayout()
        right.addWidget(QLabel("LVar tester"))
        right.addWidget(muted("Find names in MSFS Developer Mode → Tools → Behaviors → LocalVariables. "
                              "Watch them live here, then add them to the profile so they are recorded."))
        add = QHBoxLayout()
        self.lvar_edit = QLineEdit()
        self.lvar_edit.setPlaceholderText("L:A_FC_THROTTLE_LEFT_INPUT")
        self.lvar_edit.returnPressed.connect(self._watch_lvar)
        watch = QPushButton("Watch")
        watch.clicked.connect(self._watch_lvar)
        add.addWidget(self.lvar_edit, 1)
        add.addWidget(watch)
        right.addLayout(add)
        self.lvar_table = QTableWidget(0, 2)
        self.lvar_table.setHorizontalHeaderLabels(["LVar", "Value"])
        self.lvar_table.verticalHeader().setVisible(False)
        self.lvar_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.lvar_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.lvar_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        right.addWidget(self.lvar_table, 1)
        wrow = QHBoxLayout()
        self.lvar_value = QDoubleSpinBox()
        self.lvar_value.setRange(-1e6, 1e6)
        self.lvar_value.setDecimals(3)
        wbtn = QPushButton("Write value")
        wbtn.clicked.connect(self._write_lvar)
        rbtn = QPushButton("Remove")
        rbtn.clicked.connect(self._remove_lvar)
        wrow.addWidget(self.lvar_value)
        wrow.addWidget(wbtn)
        wrow.addWidget(rbtn)
        right.addLayout(wrow)
        srow = QHBoxLayout()
        b1 = QPushButton("Add selected as thrust-lever LVars")
        b1.clicked.connect(lambda: self._add_to_profile(True))
        b2 = QPushButton("Add selected as recorded LVars")
        b2.clicked.connect(lambda: self._add_to_profile(False))
        srow.addWidget(b1)
        srow.addWidget(b2)
        right.addLayout(srow)
        lay.addLayout(left, 1)
        lay.addLayout(right, 1)
        return w

    def _shortcuts(self) -> None:
        for seq, fn in (("Ctrl+R", lambda: self.rec_btn.click()), ("Space", self._toggle_play),
                        ("Home", lambda: self._seek(0.0))):
            a = QAction(self)
            a.setShortcut(QKeySequence(seq))
            a.triggered.connect(fn)
            self.addAction(a)

    # ================================================================== session events
    def _on_state(self, state: str) -> None:
        self.state = state
        self._replay_starting = False
        colors = {DISCONNECTED: theme.MUTED, IDLE: theme.ECAM_GREEN, RECORDING: theme.RED, REPLAY: theme.CYAN}
        text = {DISCONNECTED: "Disconnected", IDLE: "Connected", RECORDING: "Recording", REPLAY: "Replay"}[state]
        self.conn_lbl.setText(f"● {text}")
        self.conn_lbl.setStyleSheet(f"color: {colors[state]};")
        self.connect_btn.setText("Disconnect" if state != DISCONNECTED else "Connect")
        connected = state != DISCONNECTED
        self.rec_btn.setEnabled(connected and state != REPLAY)
        self.rec_btn.setChecked(state == RECORDING)
        self.rec_btn.setText("■  STOP REC" if state == RECORDING else "●  REC")
        self.play_btn.setEnabled(connected and state != RECORDING and self._has_flight())
        self.stop_btn.setEnabled(state == REPLAY)
        self.table.setEnabled(state != RECORDING)
        if state != REPLAY:
            self.replay_playing = False
        self._update_play_button()

    def _on_message(self, text: str, level: str) -> None:
        if level == "debug":
            self.log.appendPlainText(f"{time.strftime('%H:%M:%S')}  {text}")
            return
        self.log.appendPlainText(f"{time.strftime('%H:%M:%S')}  [{level}] {text}")
        self.status.setText(text)
        if level == "error" and "SimConnect.dll" in text:
            QMessageBox.warning(self, APP_NAME, text)

    def _on_aircraft(self, info: dict) -> None:
        self.aircraft_info = info
        title = info.get("title") or "No aircraft"
        self.aircraft_lbl.setText(f"✈  {title}")
        self.aircraft_lbl.setToolTip(info.get("path", ""))
        p: Profile = info.get("profile")
        if p is not None:
            self._show_profile(p)
            if self.profile_combo.currentData() is None:
                self.profile_combo.setItemText(0, f"Auto: {p.name}")

    def _on_live(self, values: dict) -> None:
        self.live = values
        rec = None
        if self.state == REPLAY and self.sampler is not None:
            rec = self.sampler.frame(self.replay_t)
        piston = int(values.get("meta.engine_type", 1)) == ENGINE_TYPE_PISTON
        n = int(values.get("meta.engines", 0) or 0) or 2
        for g in self.gauges:
            g.setVisible(g.engine <= max(n, 1))
            g.set_values(values, rec, piston, g.engine <= n)
        for key, lbl in self.flight_vals.items():
            v = values.get(key)
            fmt = self._flight_fmt[key]
            if v is None or not math.isfinite(v):
                lbl.setText("--")
            elif fmt.startswith("bool:"):
                on, off = fmt[5:].split("/")
                lbl.setText(on if v > 0.5 else off)
            else:
                lbl.setText(fmt.format(v))
        for key, lbl in self.light_lbls.items():
            on = values.get(key, 0) > 0.5
            lbl.setStyleSheet(f"color: {theme.BG}; background: {theme.AMBER};" if on else "")

    def _on_position(self, t: float, duration: float, playing: bool) -> None:
        self.replay_t = t
        self.replay_playing = playing and self.state == REPLAY
        self.timeline.set_position(t, duration)
        self.clock_lbl.setText(f"{fmt_time(t)} / {fmt_time(duration)}")
        self._update_play_button()
        if self.state == REPLAY:
            plan = self.session._replay_plan
            if plan:
                self.plan_lbl.setText(f"Replay: profile {plan['profile'].name} · thrust levers via "
                                      f"{plan['method']} · spool {plan['spool']} · {plan['engines']} engines")

    def _on_recorded(self, flight: Flight) -> None:
        flight.meta["title"] = f"{flight.meta.get('aircraft') or 'Flight'} · {time.strftime('%d %b %H:%M')}"
        fid = self.logbook.save(flight)
        self._refresh_logbook(select=fid)
        self._load(flight)
        self._on_message(f"Saved to logbook ({fmt_time(flight.duration)})", "info")

    def _on_probe(self, values: dict) -> None:
        self.probe_values = values
        for r in range(self.lvar_table.rowCount()):
            name = self.lvar_table.item(r, 0).text()
            v = values.get(name)
            self.lvar_table.item(r, 1).setText("--" if v is None else f"{v:.4g}")

    # ================================================================== actions
    def _toggle_connect(self) -> None:
        if self.state == DISCONNECTED:
            self.session.auto_connect = True
            self.session.connect()
        else:
            self.session.disconnect()

    def _toggle_record(self) -> None:
        if self.state == RECORDING:
            self.session.stop_record()
        else:
            self.session.record()

    def _has_flight(self) -> bool:
        return self.flight is not None or bool(self.table.selectedItems())

    def _toggle_play(self) -> None:
        if self.state == REPLAY:
            if self.replay_playing:
                self.session.pause()
                self.replay_playing = False
            else:
                self.session.resume()
                self.replay_playing = True
            self._update_play_button()
            return
        if self.state != IDLE:
            return
        self._play_selected()

    def _play_selected(self) -> None:
        if self.state == REPLAY and not self.replay_playing:
            self.session.resume()
            self.replay_playing = True
            self._update_play_button()
            return
        if self.state not in (IDLE, REPLAY):
            return
        fid = self._selected_id()
        if fid is not None and (self.flight is None or self.flight.id != fid):
            self._load(self.logbook.load(fid))
        if self.flight is None:
            return
        start = self.replay_t if 0 < self.replay_t < self.flight.duration - 0.5 else 0.0
        self.session.play(self.flight, start)
        self.replay_playing = True
        self._update_play_button()

    def _stop(self) -> None:
        self.session.stop_replay()

    def _seek(self, t: float) -> None:
        self._scrub(t)

    def _scrub(self, t: float) -> None:
        """Move the aircraft to ``t``. When no replay is running yet, start one paused,
        so dragging the timeline always moves the aircraft in the simulator."""
        self.replay_t = t
        if self.flight:
            self.clock_lbl.setText(f"{fmt_time(t)} / {fmt_time(self.flight.duration)}")
        if self.state == REPLAY or self._replay_starting:
            self.session.seek(t)
        elif self.state == IDLE and self.flight is not None:
            self._replay_starting = True   # until the session confirms, further drags are seeks
            self.session.play(self.flight, t, paused=True)
            QTimer.singleShot(1500, self._replay_start_timeout)
            self.replay_playing = False
            self._update_play_button()

    def _replay_start_timeout(self) -> None:
        if self._replay_starting and self.state != REPLAY:
            self._replay_starting = False   # session didn't start (e.g. not connected) - allow a retry

    def _update_play_button(self) -> None:
        self.play_btn.setText("❚❚  Pause" if self.replay_playing else "▶  Play")
        self.play_btn.setEnabled(self.state in (IDLE, REPLAY) and self._has_flight())

    def _apply_options(self, *_):
        if not hasattr(self, "throttle_combo"):
            return
        o = ReplayOptions(**{k: cb.isChecked() for k, cb in self.opt.items()},
                          throttle_method=self.throttle_combo.currentData(),
                          spool=self.spool_combo.currentData(),
                          engine_autostart=self.start_combo.currentData(),
                          body_velocity=self.vel_combo.currentData(),
                          rotation_rates=self.rot_check.isChecked(),
                          wing_flex=self.flex_combo.currentData(),
                          loop=self.loop.isChecked())
        self.session.set_options(o)

    # ================================================================== logbook
    def _refresh_logbook(self, select: Optional[int] = None) -> None:
        rows = self.logbook.list()
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for r, info in enumerate(rows):
            vals = [info["created"][:16], info["title"] or "", info["profile"] or "",
                    fmt_time(info["duration"] or 0), f"{info['samples'] or 0:,}"]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                item.setData(Qt.UserRole, info["id"])
                self.table.setItem(r, c, item)
            if select is not None and info["id"] == select:
                self.table.selectRow(r)
        self.table.blockSignals(False)
        self._update_play_button()

    def _selected_id(self) -> Optional[int]:
        items = self.table.selectedItems()
        return items[0].data(Qt.UserRole) if items else None

    def _on_select_flight(self) -> None:
        fid = self._selected_id()
        if fid is None or self.state == RECORDING:
            return
        if self.flight is None or self.flight.id != fid:
            try:
                self._load(self.logbook.load(fid))
            except Exception as exc:
                self._on_message(f"Could not load flight: {exc}", "error")
        self._update_play_button()

    def _load(self, flight: Flight) -> None:
        if self.state == REPLAY:
            self.session.stop_replay()
        self._replay_starting = False
        self.flight = flight
        self.sampler = flight.sampler()
        self.replay_t = 0.0
        self.timeline.set_position(0, flight.duration)
        self.clock_lbl.setText(f"00:00:00 / {fmt_time(flight.duration)}")
        extras = []
        if any(k.startswith("lvar.") for k in flight.columns):
            extras.append(f"{sum(k.startswith('lvar.') for k in flight.columns)} LVars")
        if any(k.startswith("axis.") for k in flight.columns):
            extras.append("hardware throttle axis")
        self.flight_lbl.setText(f"{flight.meta.get('title', 'Flight')}  ·  profile {flight.meta.get('profile_name', '-')}"
                                f"  ·  {flight.meta.get('engines', '?')} engines"
                                + (f"  ·  {', '.join(extras)}" if extras else ""))
        self._update_play_button()

    def _rename(self) -> None:
        fid = self._selected_id()
        if fid is None:
            return
        cur = self.table.item(self.table.currentRow(), 1).text()
        text, ok = QInputDialog.getText(self, "Rename flight", "Title:", text=cur)
        if ok:
            self.logbook.rename(fid, text)
            if self.flight and self.flight.id == fid:
                self.flight.meta["title"] = text
            self._refresh_logbook(select=fid)

    def _delete(self) -> None:
        fid = self._selected_id()
        if fid is None:
            return
        if QMessageBox.question(self, "Delete flight", "Delete the selected flight from the logbook?") \
                != QMessageBox.Yes:
            return
        if self.flight and self.flight.id == fid:
            if self.state == REPLAY:
                self.session.stop_replay()
            self.flight = None
            self.sampler = None
        self.logbook.delete(fid)
        self._refresh_logbook()

    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import flight", "", "FlyRoll flights (*.flyroll)")
        if path:
            ids = self.logbook.import_file(Path(path))
            self._refresh_logbook(select=ids[-1] if ids else None)

    def _export(self) -> None:
        fid = self._selected_id()
        if fid is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export flight", "flight.flyroll", "FlyRoll flights (*.flyroll)")
        if path:
            self.logbook.export_file(fid, Path(path))
            self._on_message(f"Exported to {path}", "info")

    def _export_csv(self) -> None:
        fid = self._selected_id()
        if fid is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export CSV", "flight.csv", "CSV (*.csv)")
        if path:
            Logbook.export_csv(self.logbook.load(fid), Path(path))
            self._on_message(f"Exported to {path}", "info")

    # ================================================================== profiles / lvars
    def _fill_profile_combo(self) -> None:
        self.profile_combo.blockSignals(True)
        cur = self.profile_combo.currentData()
        self.profile_combo.clear()
        self.profile_combo.addItem("Auto-detect aircraft", None)
        for p in self.profiles.sorted():
            self.profile_combo.addItem(p.name + ("" if p.builtin else "  (user)"), p.id)
        idx = self.profile_combo.findData(cur)
        self.profile_combo.setCurrentIndex(max(0, idx))
        self.profile_combo.blockSignals(False)

    def _on_profile_combo(self) -> None:
        self.session.set_profile_override(self.profile_combo.currentData())

    def _show_profile(self, p: Profile) -> None:
        lv = "".join(f"<li><code>{n}</code></li>" for n in p.throttle_lvars) or "<li>none</li>"
        extra = "".join(f"<li><code>{s.name}</code> – {s.replay}{' – ' + s.note if s.note else ''}</li>"
                        for s in p.lvars) or "<li>none</li>"
        self.profile_view.setHtml(
            f"<h3>{p.name}</h3><p style='color:{theme.MUTED}'>id <code>{p.id}</code> · "
            f"{'built-in' if p.builtin else 'user profile'}</p>"
            f"<p><b>Thrust levers:</b> {p.throttle_method} &nbsp; <b>Spool:</b> {p.spool} "
            f"&nbsp; <b>Auto start:</b> {'yes' if p.engine_autostart else 'no'}</p>"
            f"<p><b>Thrust-lever LVars</b></p><ul>{lv}</ul><p><b>Recorded LVars</b></p><ul>{extra}</ul>"
            f"<p><b>Matches</b>: {', '.join(p.match) or '(fallback)'}</p><p>{p.notes}</p>")

    def _open_profile_dir(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.profiles.user_dir)))

    def _reload_profiles(self) -> None:
        self.profiles.reload()
        self._fill_profile_combo()
        self.session.reload_profiles()

    def _watch_lvar(self) -> None:
        name = self.lvar_edit.text().strip()
        if not name:
            return
        if not name.upper().startswith(("L:", "Z:")):
            name = "L:" + name
        names = [self.lvar_table.item(r, 0).text() for r in range(self.lvar_table.rowCount())]
        if name not in names:
            r = self.lvar_table.rowCount()
            self.lvar_table.insertRow(r)
            self.lvar_table.setItem(r, 0, QTableWidgetItem(name))
            self.lvar_table.setItem(r, 1, QTableWidgetItem("--"))
            names.append(name)
        self.lvar_edit.clear()
        self.session.set_probe(names)

    def _selected_lvars(self) -> List[str]:
        rows = sorted({i.row() for i in self.lvar_table.selectedItems()})
        return [self.lvar_table.item(r, 0).text() for r in rows]

    def _remove_lvar(self) -> None:
        for name in self._selected_lvars():
            for r in range(self.lvar_table.rowCount()):
                if self.lvar_table.item(r, 0).text() == name:
                    self.lvar_table.removeRow(r)
                    break
        self.session.set_probe([self.lvar_table.item(r, 0).text() for r in range(self.lvar_table.rowCount())])

    def _write_lvar(self) -> None:
        for name in self._selected_lvars():
            self.session.write_lvar(name, self.lvar_value.value())

    def _add_to_profile(self, throttle: bool) -> None:
        names = self._selected_lvars()
        if not names:
            QMessageBox.information(self, APP_NAME, "Select one or more LVars in the table first.")
            return
        base: Profile = self.aircraft_info.get("profile") or self.profiles.get("default")
        data = json.loads(base.to_json())
        if base.id == "default":
            title = self.aircraft_info.get("title") or "My aircraft"
            data["id"] = "user-" + "".join(ch if ch.isalnum() else "-" for ch in title.lower()).strip("-")[:40]
            data["name"] = title
            data["match"] = [title]
        p = Profile.from_dict(data)
        if throttle:
            p.throttle_lvars = [LVarSpec(n).name for n in names]
            p.throttle_method = "lvar"
            p.write_throttle_simvar = False
        else:
            have = {s.name for s in p.lvars}
            p.lvars += [LVarSpec(n, "continuous") for n in names if LVarSpec(n).name not in have]
        path = self.profiles.save_user(p)
        self._reload_profiles()
        self._on_message(f"Profile saved: {path}", "info")

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.state == RECORDING:
            if QMessageBox.question(self, APP_NAME, "A recording is running. Stop and save it before quitting?") \
                    == QMessageBox.Yes:
                self.session.stop_record()
                # give the session thread a moment to deliver the flight
                deadline = time.time() + 2
                from PySide6.QtWidgets import QApplication
                while self.state == RECORDING and time.time() < deadline:
                    QApplication.processEvents()
                    time.sleep(0.02)
        self.session.shutdown()
        self.session.join(timeout=2)
        event.accept()
