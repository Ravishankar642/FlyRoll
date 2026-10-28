"""In-process simulated aircraft that speaks the FlyRoll backend protocol.

Used for ``FlyRoll --demo`` (try the app without MSFS) and for the automated
tests.  The interesting part is that the "fenix" and "a350" mock aircraft
reproduce the behaviour that breaks Sky Dolly on study-level airliners: their
FADEC logic re-writes ``GENERAL ENG THROTTLE LEVER POSITION`` every frame from
an internal lever state, so writing that simvar from outside has no effect.
"""
from __future__ import annotations

import math
import struct
import time
from collections import deque
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .backend import (
    F64,
    PERIOD_NEVER,
    PERIOD_ONCE,
    PERIOD_SECOND,
    Backend,
    Field,
    MsgData,
    MsgEvent,
    MsgException,
    MsgFilename,
    MsgFrame,
    MsgOpen,
    MsgSystemState,
)

AIRCRAFT = {
    "fenix": dict(title="FenixA320 IAE (mock)", atc_model="A320", atc_type="AIRBUS",
                  path=r"SimObjects\Airplanes\FNX_320_IAE\aircraft.cfg (fnx-aircraft-320)",
                  engines=2, engine_type=1, fadec="lvar"),
    "a350": dict(title="iniBuilds A350-900 (mock)", atc_model="A359", atc_type="AIRBUS",
                 path=r"SimObjects\Airplanes\inibuilds-aircraft-a350\aircraft.cfg",
                 engines=2, engine_type=1, fadec="axis"),
    "a380": dict(title="iniBuilds A380-800 (mock)", atc_model="A388", atc_type="AIRBUS",
                 path=r"SimObjects\Airplanes\inibuilds-aircraft-a380\aircraft.cfg",
                 engines=4, engine_type=1, fadec="axis"),
    "c172": dict(title="Cessna Skyhawk (mock)", atc_model="C172", atc_type="CESSNA",
                 path=r"SimObjects\Airplanes\Asobo_C172sp_AS1000\aircraft.cfg",
                 engines=1, engine_type=0, fadec=None),
}

FENIX_LEVER = ("L:A_FC_THROTTLE_LEFT_INPUT", "L:A_FC_THROTTLE_RIGHT_INPUT")


def fenix_lever_to_percent(v: float) -> float:
    """0..5 Fenix lever (2 = idle, 5 = TOGA, <2 = reverse) -> lever percent (-100..100)."""
    return (v - 2.0) / 3.0 * 100.0 if v >= 2.0 else (v - 2.0) / 2.0 * 100.0


def fenix_lever_to_n1(v: float) -> float:
    """Manual-thrust N1 for a Fenix lever position (A/THR off).

    Like the real aircraft: between IDLE (2) and CLB (3) the lever sets N1
    proportionally; FLX/MCT (4) and TOGA (5) are fixed thrust; below 2 reverse."""
    if v < 2.0:
        return 20.0 + (2.0 - v) / 2.0 * 50.0
    if v <= 3.0:
        return 20.0 + (v - 2.0) * 68.0
    return 88.0 + (min(v, 5.0) - 3.0) * 6.0


def percent_to_fenix_lever(p: float) -> float:
    return 2.0 + p / 100.0 * 3.0 if p >= 0 else 2.0 + p / 100.0 * 2.0


class MockAircraft:
    def __init__(self, kind: str = "fenix") -> None:
        self.kind = kind
        self.cfg = AIRCRAFT[kind]
        self.n_eng = self.cfg["engines"]
        self.vars: Dict[str, float] = {}
        self.lvars: Dict[str, float] = {}
        self.frozen = False
        self.lever = [0.0] * 5           # internal lever percent per engine (1-based)
        self.sim_time = 0.0
        self.lat, self.lon, self.alt = 43.6584, 7.2159, 12.0   # Nice LFMN
        self.hdg, self.pitch, self.bank = 44.0, 0.0, 0.0
        self.ias = 0.0
        self.lights = {k: 0.0 for k in ("NAV", "BEACON", "LANDING", "TAXI", "STROBE", "PANEL",
                                        "RECOGNITION", "WING", "LOGO", "CABIN")}
        self.flaps_index = 0
        self.gear = 1.0
        self.spoilers = 0.0
        self.spoilers_armed = 0.0
        self.ground_spoilers = False   # Airbus-like auto extension: armed + reverse selected
        self.parking = 1.0
        self.n1 = [0.0] * 5
        self.n2 = [0.0] * 5
        self.running = [False] * 5
        self.ext_n1: Dict[int, float] = {}
        self.athr_n1: Optional[float] = None   # A/THR active: N1 it commands when levers are in CLB
        self.writes_ignored = 0
        if self.cfg["fadec"] == "lvar":
            for name in FENIX_LEVER:
                self.lvars[name] = 2.0
        for i in range(1, self.n_eng + 1):
            self.running[i] = True
            self.n1[i] = 20.0
            self.n2[i] = 60.0

    # -- variable access ----------------------------------------------------
    def get(self, name: str) -> float:
        if name.upper().startswith(("L:", "Z:")):
            return self.lvars.get(name, 0.0)
        base, _, idx = name.partition(":")
        i = int(idx) if idx.isdigit() else 0
        eng_ok = 1 <= i <= self.n_eng
        b = base.upper()
        if b == "PLANE LATITUDE":
            return self.lat
        if b == "PLANE LONGITUDE":
            return self.lon
        if b == "PLANE ALTITUDE":
            return self.alt
        if b == "PLANE HEADING DEGREES TRUE":
            return self.hdg
        if b == "PLANE PITCH DEGREES":
            return self.pitch
        if b == "PLANE BANK DEGREES":
            return self.bank
        if b == "AIRSPEED INDICATED" or b == "GROUND VELOCITY":
            return self.ias
        if b == "SIMULATION TIME":
            return self.sim_time
        if b == "NUMBER OF ENGINES":
            return self.n_eng
        if b == "ENGINE TYPE":
            return self.cfg["engine_type"]
        if b.startswith("LIGHT "):
            return self.lights.get(b[6:], 0.0)
        if b == "FLAPS HANDLE INDEX":
            return self.flaps_index
        if b == "GEAR HANDLE POSITION":
            return self.gear
        if b == "SPOILERS HANDLE POSITION":
            return self.spoilers
        if b == "SPOILERS ARMED":
            return self.spoilers_armed
        if b in ("SPOILERS LEFT POSITION", "SPOILERS RIGHT POSITION"):
            return 100.0 if self.ground_spoilers else self.spoilers
        if b == "BRAKE PARKING POSITION":
            return self.parking
        if b == "GENERAL ENG THROTTLE LEVER POSITION":
            return self.lever[i] if eng_ok else 0.0
        if b == "TURB ENG N1" or b == "TURB ENG CORRECTED N1":
            return self.n1[i] if eng_ok else 0.0
        if b == "TURB ENG N2":
            return self.n2[i] if eng_ok else 0.0
        if b == "GENERAL ENG RPM":
            return self.n1[i] * 27.0 if eng_ok else 0.0
        if b == "GENERAL ENG COMBUSTION":
            return 1.0 if eng_ok and self.running[i] else 0.0
        if b == "TURB ENG FUEL FLOW PPH":
            return 400 + self.n1[i] * 60 if eng_ok and self.running[i] else 0.0
        if b == "GENERAL ENG EXHAUST GAS TEMPERATURE":
            return 300 + self.n1[i] * 5 if eng_ok else 15.0
        if b == "TURB ENG REVERSE NOZZLE PERCENT":
            return 100.0 if eng_ok and self.lever[i] < -5 else 0.0
        return self.vars.get(name, 0.0)

    def set(self, name: str, value: float) -> None:
        if name.upper().startswith(("L:", "Z:")):
            self.lvars[name] = value
            return
        base, _, idx = name.partition(":")
        i = int(idx) if idx.isdigit() else 0
        b = base.upper()
        if b == "PLANE LATITUDE":
            self.lat = value
        elif b == "PLANE LONGITUDE":
            self.lon = value
        elif b == "PLANE ALTITUDE":
            self.alt = value
        elif b == "PLANE HEADING DEGREES TRUE":
            self.hdg = value
        elif b == "PLANE PITCH DEGREES":
            self.pitch = value
        elif b == "PLANE BANK DEGREES":
            self.bank = value
        elif b == "GENERAL ENG THROTTLE LEVER POSITION" and 1 <= i <= 4:
            if self.cfg["fadec"]:
                self.writes_ignored += 1   # FADEC will overwrite it next frame (the Sky Dolly problem)
            self.lever[i] = value
        elif b in ("TURB ENG N1", "TURB ENG CORRECTED N1") and 1 <= i <= 4:
            if self.cfg["fadec"] == "lvar":
                self.writes_ignored += 1   # Fenix runs its own engine model: N1 writes don't stick
            else:
                self.n1[i] = value
        elif b == "TURB ENG N2" and 1 <= i <= 4:
            self.n2[i] = value
        else:
            self.vars[name] = value

    # -- input events (keys / axes) -------------------------------------------
    def event(self, name: str, data: int) -> None:
        n = name.upper()
        if n.startswith("THROTTLE") and "AXIS_SET_EX1" in n:
            eng = n[8] if n[8].isdigit() else ""
            pct = (data + 16384) / 32767.0 * 100.0
            self._hardware_lever([int(eng)] if eng else range(1, self.n_eng + 1), pct)
        elif n.startswith("THROTTLE") and n.endswith("_SET") and "AXIS" not in n:
            eng = n[8] if n[8].isdigit() else ""
            self._hardware_lever([int(eng)] if eng else range(1, self.n_eng + 1), data / 16383.0 * 100.0)
        elif n.startswith("TOGGLE_") and n.endswith("_LIGHTS") or n in ("LANDING_LIGHTS_TOGGLE", "STROBES_TOGGLE",
                                                                          "PANEL_LIGHTS_TOGGLE"):
            key = {"LANDING_LIGHTS_TOGGLE": "LANDING", "STROBES_TOGGLE": "STROBE",
                   "PANEL_LIGHTS_TOGGLE": "PANEL"}.get(n) or n[len("TOGGLE_"):-len("_LIGHTS")]
            if key in self.lights:
                self.lights[key] = 0.0 if self.lights[key] else 1.0
        elif n == "FLAPS_INCR":
            self.flaps_index = min(self.flaps_index + 1, 4)
        elif n == "FLAPS_DECR":
            self.flaps_index = max(self.flaps_index - 1, 0)
        elif n == "GEAR_UP":
            self.gear = 0.0
        elif n == "GEAR_DOWN":
            self.gear = 1.0
        elif n == "SPOILERS_ARM_SET":
            self.spoilers_armed = float(data)
            if not data:
                self.ground_spoilers = False
        elif n == "SPOILERS_SET":
            self.spoilers = data / 16383.0 * 100.0
        elif n == "SPOILERS_OFF":
            self.spoilers = 0.0
        elif n == "SPOILERS_ON":
            self.spoilers = 100.0
        elif n == "PARKING_BRAKE_SET":
            self.parking = float(data)
        elif n == "FREEZE_LATITUDE_LONGITUDE_SET":
            self.frozen = bool(data)
        elif n == "ENGINE_AUTO_START":
            for i in range(1, self.n_eng + 1):
                self.running[i] = True

    def _hardware_lever(self, engines, pct: float) -> None:
        for i in engines:
            if self.cfg["fadec"] == "lvar":
                self.lvars[FENIX_LEVER[i - 1]] = percent_to_fenix_lever(pct)
            else:
                self.lever[i] = pct
                self.vars[f"_internal_lever:{i}"] = pct

    # -- physics ------------------------------------------------------------
    def step(self, dt: float, paused: bool = False) -> None:
        if paused:
            return
        self.sim_time += dt
        for i in range(1, self.n_eng + 1):
            fadec = self.cfg["fadec"]
            if fadec == "lvar":
                # Fenix-like: lever simvar is DERIVED from the LVar every frame
                self.lever[i] = fenix_lever_to_percent(self.lvars.get(FENIX_LEVER[i - 1], 2.0))
            elif fadec == "axis":
                # iniBuilds-like: internal lever state only changes via hardware axis input
                self.lever[i] = self.vars.get(f"_internal_lever:{i}", 0.0)
            if self.running[i]:
                if fadec == "lvar":
                    lv = self.lvars.get(FENIX_LEVER[i - 1], 2.0)
                    if self.athr_n1 is not None and 2.95 <= lv <= 3.05:
                        target = self.athr_n1          # autothrust managing thrust in the CLB detent
                    elif self.athr_n1 is not None and 2.0 < lv < 2.95:
                        target = min(self.athr_n1, fenix_lever_to_n1(lv))   # lever = thrust limit
                    else:
                        target = fenix_lever_to_n1(lv)
                else:
                    target = 20.0 + max(self.lever[i], 0.0) * 0.8 + max(-self.lever[i], 0.0) * 0.5
                self.n1[i] += (target - self.n1[i]) * min(1.0, dt / 2.5)
                self.n2[i] += (60 + target * 0.4 - self.n2[i]) * min(1.0, dt / 2.0)
        if self.spoilers_armed and any(self.lever[i] < -5 for i in range(1, self.n_eng + 1)):
            self.ground_spoilers = True
        if not self.frozen:
            for side in (1, 2):
                self.vars[f"WING FLEX PCT:{side}"] = 0.02 + 0.1 * self.ias / 250.0
            avg = sum(self.n1[1:self.n_eng + 1]) / self.n_eng
            accel = (avg - 30.0) * 0.12
            self.ias = max(0.0, min(250.0, self.ias + accel * dt))
            if self.ias > 140:
                self.pitch = -6.0
                self.alt += self.ias * 0.1 * dt * 10
            fps = self.ias * 1.68781
            d = fps * dt / 364000.0
            self.lat += d * math.cos(math.radians(self.hdg))
            self.lon += d * math.sin(math.radians(self.hdg)) / max(0.2, math.cos(math.radians(self.lat)))


class MockBackend(Backend):
    """SimConnect stand-in. ``clock`` may be replaced for deterministic tests."""

    name = "Demo simulator"
    FRAME_RATE = 60.0

    def __init__(self, kind: str = "fenix", clock: Callable[[], float] = time.monotonic,
                 pilot: bool = True, realtime: bool = True) -> None:
        self.ac = MockAircraft(kind)
        self.clock = clock
        self.realtime = realtime
        self.pilot = pilot
        self._open = False
        self._defs: Dict[int, List[Field]] = {}
        self._requests: Dict[int, Tuple[int, int, float]] = {}  # req -> (def, period, last_sent)
        self._events: Dict[int, str] = {}
        self._listened: Dict[int, int] = {}       # event id -> group
        self._system: Dict[int, str] = {}
        self._queue: deque = deque()
        self._last = 0.0
        self._pilot_t = 0.0
        self.paused = False
        self.transmitted: List[Tuple[str, int]] = []
        self.reject: set = set()          # simvar names whose writes raise a SimConnect exception
        self._send_id = 0

    # -- lifecycle ----------------------------------------------------------
    def open(self, app_name: str) -> None:
        self._open = True
        self._last = self.clock()
        self._queue.append(MsgOpen("Mock Flight Simulator", "12.1"))

    def close(self) -> None:
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    # -- definitions --------------------------------------------------------
    def add_definition(self, define_id: int, fields: Sequence[Field]) -> None:
        self._defs.setdefault(define_id, []).extend(fields)

    def clear_definition(self, define_id: int) -> None:
        self._defs.pop(define_id, None)

    def request_data(self, request_id: int, define_id: int, period: int, flags: int = 0) -> None:
        if period == PERIOD_NEVER:
            self._requests.pop(request_id, None)
        elif period == PERIOD_ONCE:
            self._queue.append(self._data(request_id, define_id))
        else:
            self._requests[request_id] = (define_id, period, -1e9)

    def set_data(self, define_id: int, values: Sequence[float]) -> None:
        self._send_id += 1
        for i, (f, v) in enumerate(zip(self._defs.get(define_id, []), values)):
            if f.name in self.reject:
                self._queue.append(MsgException(3, self._send_id, i + 1))  # DATA_ERROR-like
                continue
            self.ac.set(f.name, float(v))

    def last_send_id(self) -> int:
        return self._send_id

    def map_event(self, event_id: int, name: str) -> None:
        self._events[event_id] = name

    def transmit(self, event_id: int, data: int = 0) -> None:
        name = self._events.get(event_id, "")
        self.transmitted.append((name, int(data)))
        self.ac.event(name, int(data))
        if event_id in self._listened:      # real SimConnect also echoes our own events
            self._queue.append(MsgEvent(event_id, int(data)))

    def listen_event(self, group_id: int, event_id: int) -> None:
        self._listened[event_id] = group_id

    def subscribe_system_event(self, event_id: int, name: str) -> None:
        self._system[event_id] = name

    def unsubscribe_system_event(self, event_id: int) -> None:
        self._system.pop(event_id, None)

    def request_system_state(self, request_id: int, state: str) -> None:
        if state == "AircraftLoaded":
            self._queue.append(MsgSystemState(request_id, 0, 0.0, self.ac.cfg["path"]))

    def set_paused(self, paused: bool) -> None:
        """Pause the mock simulator (sends the 'Pause' system event like MSFS)."""
        self.paused = paused
        for eid, name in self._system.items():
            if name.lower() == "pause":
                self._queue.append(MsgEvent(eid, 1 if paused else 0))

    # -- simulated hardware -------------------------------------------------------
    def hardware_throttle(self, pct: float, engine: int = 0) -> None:
        """Pretend the user moved a throttle quadrant axis (sends THROTTLEx_AXIS_SET_EX1)."""
        name = f"THROTTLE{engine}_AXIS_SET_EX1" if engine else "THROTTLE_AXIS_SET_EX1"
        value = int(round(pct / 100.0 * 32767 - 16384))
        self.ac.event(name, value)
        for eid, ename in self._events.items():
            if ename == name and eid in self._listened:
                self._queue.append(MsgEvent(eid, value))

    def _pilot(self, t: float) -> None:
        """Scripted demo flight: lights on, TOGA after 5 s, gear up at 40 s, CLB at 50 s."""
        script = [(1.0, lambda: self.ac.event("TOGGLE_BEACON_LIGHTS", 0)),
                  (2.0, lambda: self.ac.event("STROBES_TOGGLE", 0)),
                  (2.5, lambda: self.ac.event("LANDING_LIGHTS_TOGGLE", 0)),
                  (3.0, lambda: self.ac.event("FLAPS_INCR", 0)),
                  (5.0, lambda: self.hardware_throttle(40)),
                  (7.0, lambda: self.hardware_throttle(100)),
                  (40.0, lambda: self.ac.event("GEAR_UP", 0)),
                  (50.0, lambda: self.hardware_throttle(70)),
                  (55.0, lambda: self.ac.event("FLAPS_DECR", 0))]
        for at, fn in script:
            if self._pilot_t < at <= t:
                fn()
        self._pilot_t = t

    # -- message pump ---------------------------------------------------------
    def _data(self, request_id: int, define_id: int) -> MsgData:
        out = bytearray()
        for f in self._defs.get(define_id, []):
            if f.kind == F64:
                out += struct.pack("<d", float(self.ac.get(f.name)))
            else:
                text = {"TITLE": self.ac.cfg["title"], "ATC MODEL": self.ac.cfg["atc_model"],
                        "ATC TYPE": self.ac.cfg["atc_type"]}.get(f.name.upper(), "")
                out += text.encode()[:255].ljust(256, b"\0")
        return MsgData(request_id, define_id, bytes(out))

    def wait(self, timeout_s: float) -> None:
        if self.realtime:
            time.sleep(min(timeout_s, 1.0 / self.FRAME_RATE / 2))

    def poll(self) -> Iterable[object]:
        if not self._open:
            return []
        now = self.clock()
        frames = 0
        while now - self._last >= 1.0 / self.FRAME_RATE and frames < 10:
            self._last += 1.0 / self.FRAME_RATE
            frames += 1
            self._frame(1.0 / self.FRAME_RATE)
        if now - self._last > 1.0:  # don't try to catch up after a long stall
            self._last = now
        out = list(self._queue)
        self._queue.clear()
        return out

    def _frame(self, dt: float) -> None:
        if self.pilot and not self.ac.frozen:
            self._pilot(self.ac.sim_time + dt)
        self.ac.step(dt, self.paused)
        for eid, name in self._system.items():
            if name.lower() == "frame":
                self._queue.append(MsgFrame(eid, self.FRAME_RATE, 1.0))
        for rid, (did, period, last) in list(self._requests.items()):
            if period == PERIOD_SECOND:
                if self.ac.sim_time - last < 1.0:
                    continue
            self._requests[rid] = (did, period, self.ac.sim_time)
            self._queue.append(self._data(rid, did))
