"""The FlyRoll session: connection, recording and replay.

Runs in its own thread.  The UI talks to it through thread-safe command
methods (``connect()``, ``record()``, ``play(flight)`` ...) and receives
results through a :class:`SessionListener`.

Why replay of engines works here (and not in Sky Dolly)
--------------------------------------------------------
1. *Nothing to replay*: Sky Dolly never records N1/N2/RPM/prop RPM/EGT/fuel
   flow - only lever positions.  FlyRoll records ~16 engine values per engine.
2. *Writes are overwritten*: Sky Dolly replays the throttle by writing
   ``GENERAL ENG THROTTLE LEVER POSITION``.  FADEC airliners (Fenix,
   iniBuilds, FBW ...) recompute that simvar every frame from their own
   lever state, so the write is lost and the levers stay at idle.  FlyRoll
   drives the aircraft's *own* inputs instead: its thrust-lever LVars
   (method ``lvar``) or the recorded hardware throttle axis events
   (method ``axis``), and keeps N1/N2/RPM in sync (spool ``assist``/``force``).
3. Lights: Sky Dolly's replay fed the *taxi* state into the strobe light
   (copy/paste slip in EventStateHandler::sendLight).  FlyRoll maps every
   light to its own state.
"""
from __future__ import annotations

import math
import queue
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import numpy as np

from .. import __version__
from ..sim.backend import (
    PERIOD_NEVER,
    PERIOD_SIM_FRAME,
    PERIOD_SECOND,
    STR256,
    Backend,
    Field,
    MsgData,
    MsgEvent,
    MsgException,
    MsgFilename,
    MsgFrame,
    MsgOpen,
    MsgQuit,
    MsgSystemState,
)
from .channels import (
    AXIS,
    AXIS_INPUT_EVENTS,
    ENGINE_TYPE_JET,
    ENGINE_TYPE_PISTON,
    ENGINE_TYPE_TURBOPROP,
    LIGHT_DEFS,
    MAX_ENGINES,
    POSE_KEYS,
    ROTATION_KEYS,
    STEP,
    VELOCITY_KEYS,
    Channel,
    base_channels,
    engine_key,
    lvar_channel,
)
from .flight import Flight, RecordBuffer, Sampler, smoothed
from .profiles import DEFAULT_PROFILE, Profile, ProfileStore

# data definition / request ids
DEF_INFO, DEF_RECORD, DEF_POSE, DEF_LEVERS, DEF_SPOOL, DEF_LVAR_WRITE, DEF_PROBE, DEF_PROBE_WRITE, \
    DEF_WINGFLEX = range(1, 10)
GROUP_AXIS = 1
SYS_FRAME, SYS_AIRCRAFT_LOADED, SYS_PAUSE, SYS_SIM = 50, 51, 52, 53
REQ_AIRCRAFT_PATH = 60
FIRST_EVENT_ID = 100

INFO_FIELDS = [Field("TITLE", "", STR256), Field("ATC MODEL", "", STR256), Field("ATC TYPE", "", STR256)]

DISCONNECTED, IDLE, RECORDING, REPLAY = "disconnected", "connected", "recording", "replay"

AXIS_MAX = 16383


@dataclass
class ReplayOptions:
    """User-adjustable replay settings (override the profile where set)."""

    position: bool = True
    controls: bool = True
    lights: bool = True
    flaps: bool = True
    gear: bool = True
    spoilers: bool = True
    engines: bool = True
    throttle_method: str = "profile"   # profile | auto | simvar | lvar | axis
    spool: str = "profile"             # profile | off | assist | force
    engine_autostart: str = "profile"  # profile | on | off
    body_velocity: str = "smoothed"    # smoothed | raw | off  (VELOCITY BODY X/Y/Z)
    rotation_rates: bool = False       # ROTATION VELOCITY BODY X/Y/Z (Sky Dolly doesn't write them either)
    wing_flex: str = "recorded"        # recorded | physics
    loop: bool = False


class SessionListener:
    """Callbacks (invoked from the session thread)."""

    def on_state(self, state: str) -> None: ...
    def on_message(self, text: str, level: str = "info") -> None: ...
    def on_aircraft(self, info: dict) -> None: ...
    def on_live(self, values: Dict[str, float]) -> None: ...
    def on_recorded(self, flight: Flight) -> None: ...
    def on_replay_position(self, t: float, duration: float, playing: bool) -> None: ...
    def on_probe(self, values: Dict[str, float]) -> None: ...


class Session(threading.Thread):
    def __init__(self, backend_factory: Callable[[], Backend], profiles: ProfileStore,
                 listener: Optional[SessionListener] = None, clock: Callable[[], float] = time.monotonic,
                 sample_rate: float = 30.0) -> None:
        super().__init__(name="FlyRollSession", daemon=True)
        self.backend_factory = backend_factory
        self.profiles = profiles
        self.listener = listener or SessionListener()
        self.clock = clock
        self.sample_rate = sample_rate
        self.options = ReplayOptions()
        self.auto_connect = True

        self._cmd: "queue.Queue[tuple]" = queue.Queue()
        self._quit = threading.Event()
        self.backend: Optional[Backend] = None
        self.state = DISCONNECTED
        self._last_connect_try = -1e9

        # aircraft / profile
        self.aircraft: Dict[str, str] = {"title": "", "atc_model": "", "atc_type": "", "path": ""}
        self.profile: Profile = DEFAULT_PROFILE
        self.profile_override: Optional[str] = None
        self.record_channels: List[Channel] = []
        self.live: Dict[str, float] = {}
        self._live_emit = 0.0

        # events
        self._event_ids: Dict[str, int] = {}
        self._axis_events: Dict[int, tuple] = {}

        # recording
        self.buffer: Optional[RecordBuffer] = None
        self._axis_state = np.full(MAX_ENGINES, np.nan)
        self._rec_t = 0.0
        self._rec_last_sim = None
        self._rec_last_wall = 0.0
        self._rec_meta: dict = {}
        self._paused_sim = False

        # replay
        self.flight: Optional[Flight] = None
        self.sampler: Optional[Sampler] = None
        self.replay_t = 0.0
        self.replay_speed = 1.0
        self.replay_playing = False
        self._replay_wall0 = 0.0
        self._replay_t0 = 0.0
        self._last_replay_update = 0.0
        self._sent: Dict[str, float] = {}
        self._cooldown: Dict[str, float] = {}
        self._replay_plan: dict = {}
        self._frame_subscribed = False
        self._send_tags: Dict[int, str] = {}
        self._failed_tags: set = set()
        self._logged_exceptions: set = set()
        self._spoiler_rearm_at: Optional[float] = None

        # lvar tester
        self.probe_names: List[str] = []

    # ======================================================================
    # Public, thread-safe API (called from the UI thread)
    # ======================================================================
    def connect(self) -> None: self._cmd.put(("connect",))
    def disconnect(self) -> None: self._cmd.put(("disconnect",))
    def record(self) -> None: self._cmd.put(("record",))
    def stop_record(self) -> None: self._cmd.put(("stop_record",))
    def play(self, flight: Flight, start: float = 0.0, paused: bool = False) -> None:
        self._cmd.put(("play", flight, start, paused))
    def pause(self) -> None: self._cmd.put(("pause",))
    def resume(self) -> None: self._cmd.put(("resume",))
    def seek(self, t: float) -> None: self._cmd.put(("seek", t))
    def stop_replay(self) -> None: self._cmd.put(("stop_replay",))
    def set_speed(self, speed: float) -> None: self._cmd.put(("speed", speed))
    def set_options(self, options: ReplayOptions) -> None: self._cmd.put(("options", options))
    def set_profile_override(self, profile_id: Optional[str]) -> None: self._cmd.put(("profile", profile_id))
    def reload_profiles(self) -> None: self._cmd.put(("reload_profiles",))
    def set_probe(self, names: List[str]) -> None: self._cmd.put(("probe", list(names)))
    def write_lvar(self, name: str, value: float) -> None: self._cmd.put(("write_lvar", name, value))
    def shutdown(self) -> None:
        self._cmd.put(("shutdown",))
        self._quit.set()

    # ======================================================================
    # Thread main loop
    # ======================================================================
    def run(self) -> None:
        while not self._quit.is_set():
            try:
                self._process_commands()
                if self.backend and self.backend.is_open:
                    self.backend.wait(0.004)
                    for msg in self.backend.poll():
                        self._dispatch(msg)
                    self._replay_tick_fallback()
                    self._emit_live()
                else:
                    if self.auto_connect and self.clock() - self._last_connect_try > 3.0:
                        self._do_connect(quiet=True)
                    time.sleep(0.05)
            except Exception as exc:  # keep the thread alive, report the problem
                self._msg(f"Internal error: {exc}", "error")
                traceback.print_exc()
                time.sleep(0.2)
        self._do_disconnect()

    def step(self) -> None:
        """Run one iteration synchronously (used by tests instead of start())."""
        self._process_commands()
        if self.backend and self.backend.is_open:
            for msg in self.backend.poll():
                self._dispatch(msg)
            self._replay_tick_fallback()
            self._emit_live(force=True)
        elif self.auto_connect:
            self._do_connect(quiet=True)

    # ======================================================================
    # Commands
    # ======================================================================
    def _process_commands(self) -> None:
        cmds = []
        while True:
            try:
                cmds.append(self._cmd.get_nowait())
            except queue.Empty:
                break
        # While the timeline is being dragged the UI sends a stream of seeks:
        # only the last one of every consecutive run matters.
        cmds = [c for i, c in enumerate(cmds)
                if not (c[0] == "seek" and i + 1 < len(cmds) and cmds[i + 1][0] == "seek")]
        for cmd in cmds:
            name, args = cmd[0], cmd[1:]
            if name == "connect":
                self._do_connect()
            elif name == "disconnect":
                self.auto_connect = False
                self._do_disconnect()
            elif name == "record":
                self._start_recording()
            elif name == "stop_record":
                self._stop_recording()
            elif name == "play":
                self._start_replay(*args)
            elif name == "pause":
                self._pause_replay(True)
            elif name == "resume":
                self._pause_replay(False)
            elif name == "seek":
                self._seek(float(args[0]))
            elif name == "stop_replay":
                self._stop_replay()
            elif name == "speed":
                self.replay_t = self._replay_time()
                self.replay_speed = max(0.05, float(args[0]))
                self._rebase_clock()
            elif name == "options":
                self.options = args[0]
                if self.state == REPLAY:
                    self._plan_replay()
            elif name == "profile":
                self.profile_override = args[0]
                self._select_profile()
            elif name == "reload_profiles":
                self.profiles.reload()
                self._select_profile()
            elif name == "probe":
                self._set_probe(args[0])
            elif name == "write_lvar":
                self._write_lvar(*args)
            elif name == "shutdown":
                self._quit.set()

    # ======================================================================
    # Connection
    # ======================================================================
    def _do_connect(self, quiet: bool = False) -> None:
        self._last_connect_try = self.clock()
        if self.backend and self.backend.is_open:
            return
        try:
            backend = self.backend_factory()
            backend.open("FlyRoll")
        except Exception as exc:
            if not quiet:
                self._msg(str(exc), "error")
            return
        self.backend = backend
        self._event_ids.clear()
        self._axis_events.clear()
        b = backend
        b.add_definition(DEF_INFO, INFO_FIELDS)
        b.request_data(DEF_INFO, DEF_INFO, PERIOD_SECOND)
        b.subscribe_system_event(SYS_AIRCRAFT_LOADED, "AircraftLoaded")
        b.subscribe_system_event(SYS_PAUSE, "Pause")
        b.request_system_state(REQ_AIRCRAFT_PATH, "AircraftLoaded")
        for name in ["FREEZE_LATITUDE_LONGITUDE_SET", "FREEZE_ALTITUDE_SET", "FREEZE_ATTITUDE_SET",
                     "ENGINE_AUTO_START", "ENGINE_AUTO_SHUTDOWN", "AXIS_AILERONS_SET", "AXIS_ELEVATOR_SET",
                     "AXIS_RUDDER_SET", "FLAPS_INCR", "FLAPS_DECR", "SPOILERS_SET", "SPOILERS_ARM_SET",
                     "SPOILERS_ON", "SPOILERS_OFF", "AXIS_SPOILER_SET",
                     "GEAR_UP", "GEAR_DOWN", "PARKING_BRAKE_SET", "SET_TAIL_HOOK_HANDLE", "SET_WING_FOLD",
                     *[e for _, _, e in LIGHT_DEFS],
                     *[f"THROTTLE{i}_AXIS_SET_EX1" for i in range(1, 5)],
                     *[f"THROTTLE{i}_SET" for i in range(1, 5)]]:
            self._event(name)
        # observe hardware throttle input
        for name, engine, kind in AXIS_INPUT_EVENTS:
            eid = self._event(name)
            self._axis_events[eid] = (engine, kind)
            b.listen_event(GROUP_AXIS, eid)
        self._set_state(IDLE)
        self._msg(f"Connected to {b.name}")
        self._select_profile(force=True)
        if self.probe_names:
            self._set_probe(self.probe_names)

    def _do_disconnect(self) -> None:
        if self.state == RECORDING:
            self._stop_recording()
        if self.state == REPLAY:
            self._stop_replay()
        if self.backend:
            try:
                self.backend.close()
            except Exception:
                pass
        self.backend = None
        self._frame_subscribed = False
        if self.state != DISCONNECTED:
            self._set_state(DISCONNECTED)
            self._msg("Disconnected from the simulator")

    def _event(self, name: str) -> int:
        eid = self._event_ids.get(name)
        if eid is None:
            eid = FIRST_EVENT_ID + len(self._event_ids)
            self._event_ids[name] = eid
            self.backend.map_event(eid, name)
        return eid

    def _send(self, name: str, data: int = 0) -> None:
        self.backend.transmit(self._event(name), int(data))

    # ======================================================================
    # Aircraft + profile
    # ======================================================================
    def _select_profile(self, force: bool = False) -> None:
        if self.profile_override:
            profile = self.profiles.get(self.profile_override)
        else:
            a = self.aircraft
            profile = self.profiles.detect(a["path"], a["title"], a["atc_model"], a["atc_type"])
        changed = force or profile.id != self.profile.id or profile is not self.profile
        self.profile = profile
        if changed and self.backend and self.state != RECORDING:
            self._build_record_definition()
        self.listener.on_aircraft({**self.aircraft, "profile_id": profile.id, "profile_name": profile.name,
                                   "profile": profile})

    def _build_record_definition(self) -> None:
        channels = base_channels() + [lvar_channel(s.name, STEP if s.replay == "discrete" else "linear")
                                      for s in self.profile.all_lvars()]
        self.record_channels = channels
        b = self.backend
        b.request_data(DEF_RECORD, DEF_RECORD, PERIOD_NEVER)
        b.clear_definition(DEF_RECORD)
        b.add_definition(DEF_RECORD, [Field("SIMULATION TIME", "seconds")] + [c.field() for c in channels])
        b.request_data(DEF_RECORD, DEF_RECORD, PERIOD_SIM_FRAME)

    # ======================================================================
    # Message handling
    # ======================================================================
    def _dispatch(self, msg) -> None:
        if isinstance(msg, MsgData):
            if msg.request_id == DEF_RECORD:
                self._on_record_data(msg.payload)
            elif msg.request_id == DEF_INFO:
                self._on_info(msg.payload)
            elif msg.request_id == DEF_PROBE:
                n = len(self.probe_names)
                vals = np.frombuffer(msg.payload[: n * 8], dtype="<f8")
                self.listener.on_probe(dict(zip(self.probe_names, map(float, vals))))
        elif isinstance(msg, MsgFrame):
            if self.state == REPLAY:
                self._replay_update()
        elif isinstance(msg, MsgEvent):
            if msg.event_id in self._axis_events:
                self._on_axis(msg.event_id, msg.data)
            elif msg.event_id == SYS_PAUSE:
                self._paused_sim = bool(msg.data)
        elif isinstance(msg, (MsgFilename, MsgSystemState)):
            path = msg.filename if isinstance(msg, MsgFilename) else msg.text
            if path and path != self.aircraft["path"]:
                self.aircraft["path"] = path
                self._select_profile()
        elif isinstance(msg, MsgException):
            self._on_exception(msg)
        elif isinstance(msg, MsgQuit):
            self._msg("The simulator has quit")
            self._do_disconnect()
        elif isinstance(msg, MsgOpen):
            self._msg(f"Simulator: {msg.app_name} {msg.app_version}")

    def _set_data(self, define_id: int, values, tag: str) -> None:
        """SetDataOnSimObject + remember which feature sent it, so a SimConnect
        exception can switch exactly that feature off instead of spamming."""
        self.backend.set_data(define_id, values)
        sid = self.backend.last_send_id()
        if sid:
            self._send_tags[sid] = tag
            if len(self._send_tags) > 2000:
                for k in list(self._send_tags)[:1000]:
                    del self._send_tags[k]

    def _on_exception(self, msg: MsgException) -> None:
        tag = self._send_tags.get(msg.send_id, "")
        if tag == "wingflex":
            if tag not in self._failed_tags:
                self._failed_tags.add(tag)
                self._msg("Wing flex can't be written in this simulator/aircraft - the flight model's own "
                          "wing flex is used instead", "warning")
            return
        key = (tag, msg.code, msg.index)
        if key not in self._logged_exceptions:
            self._logged_exceptions.add(key)
            self._msg(f"SimConnect exception {msg.code} ({tag or 'request'}, parameter {msg.index}, "
                      f"send id {msg.send_id})", "debug")

    def _on_info(self, payload: bytes) -> None:
        def s(raw: bytes) -> str:
            return raw.split(b"\0", 1)[0].decode("utf-8", "replace")
        title, model, typ = s(payload[0:256]), s(payload[256:512]), s(payload[512:768])
        if (title, model, typ) != (self.aircraft["title"], self.aircraft["atc_model"], self.aircraft["atc_type"]):
            self.aircraft.update(title=title, atc_model=model, atc_type=typ)
            self._select_profile()

    def _on_record_data(self, payload: bytes) -> None:
        n = 1 + len(self.record_channels)
        if len(payload) < n * 8:
            return
        vals = np.frombuffer(payload[: n * 8], dtype="<f8")
        self.live = dict(zip((c.key for c in self.record_channels), map(float, vals[1:])))
        self.live["sim.time"] = float(vals[0])
        if self.state == RECORDING:
            self._record_sample(float(vals[0]), vals[1:])

    def _on_axis(self, event_id: int, data: int) -> None:
        if self.state == REPLAY:
            return  # our own replayed events echo back - ignore
        engine, kind = self._axis_events[event_id]
        value = data if kind == "axis" else data * 2 - 16384  # 0..16383 -> -16384..16383
        value = float(max(-16384, min(16383, value)))
        if engine == 0:
            self._axis_state[:] = value
        elif 1 <= engine <= MAX_ENGINES:
            self._axis_state[engine - 1] = value
        self.live[f"axis.throttle.{engine or 1}"] = value

    def _emit_live(self, force: bool = False) -> None:
        now = self.clock()
        if force or now - self._live_emit >= 0.1:
            self._live_emit = now
            if self.live:
                self.listener.on_live(dict(self.live))
            if self.state == REPLAY and self.flight is not None:
                self.listener.on_replay_position(self._replay_time(), self.flight.duration, self.replay_playing)
            elif self.state == RECORDING:
                self.listener.on_replay_position(self._rec_t, self._rec_t, True)

    # ======================================================================
    # Recording
    # ======================================================================
    def _start_recording(self) -> None:
        if self.state == REPLAY:
            self._stop_replay()
        if self.state != IDLE:
            self._msg("Connect to the simulator first", "warning")
            return
        self.buffer = RecordBuffer(self.record_channels + AXIS)
        self._axis_state[:] = np.nan
        self._rec_t = 0.0
        self._rec_last_sim = None
        self._rec_last_wall = self.clock()
        self._rec_progress_wall = self.clock()
        self._rec_wall_mode = False
        p = self.profile
        self._rec_meta = {
            "title": self.aircraft["title"] or "Unknown aircraft",
            "aircraft": self.aircraft["title"],
            "aircraft_path": self.aircraft["path"],
            "atc_model": self.aircraft["atc_model"],
            "profile_id": p.id,
            "profile_name": p.name,
            "sample_rate": self.sample_rate,
            "app": f"FlyRoll {__version__}",
            "backend": self.backend.name if self.backend else "",
        }
        self._set_state(RECORDING)
        self._msg(f"Recording - profile: {p.name}")

    def _record_sample(self, sim_time: float, values: np.ndarray) -> None:
        """Timestamps follow SIMULATION TIME (stops while paused, follows sim rate).

        If the simulator never advances that variable we fall back to the wall
        clock (minus the time spent in pause)."""
        wall = self.clock()
        if self._rec_last_sim is None:
            t = 0.0
        else:
            if self._rec_wall_mode:
                delta = 0.0 if self._paused_sim else min(max(wall - self._rec_last_wall, 0.0), 1.0)
            else:
                delta = sim_time - self._rec_last_sim
                if delta <= 0.0 and not self._paused_sim and wall - self._rec_progress_wall > 2.0:
                    self._rec_wall_mode = True
                    self._msg("SIMULATION TIME is not advancing - using the system clock for timestamps",
                              "warning")
                    delta = min(max(wall - self._rec_last_wall, 0.0), 1.0)
                elif delta > 5.0:  # sim time jumped (flight reset / time change)
                    delta = min(max(wall - self._rec_last_wall, 0.0), 1.0)
            if delta <= 0.0:
                self._rec_last_wall = wall   # paused: no new sample
                return
            t = self._rec_t + delta
            self._rec_progress_wall = wall
        self._rec_last_sim = sim_time
        self._rec_last_wall = wall
        self._rec_t = t
        if self.buffer.count and t - self.buffer.last_time() < 1.0 / self.sample_rate - 1e-6:
            return
        self.buffer.append(t, np.concatenate([values, self._axis_state]))

    def _stop_recording(self) -> None:
        if self.state != RECORDING or self.buffer is None:
            return
        meta = dict(self._rec_meta)
        meta["engines"] = int(self.live.get("meta.engines", 0) or 0)
        meta["engine_type"] = int(self.live.get("meta.engine_type", ENGINE_TYPE_JET))
        flight = self.buffer.to_flight(meta)
        # drop axis channels that never saw any input (keeps the file small & clear)
        for c in AXIS:
            if not np.isfinite(flight.columns[c.key]).any():
                flight.columns.pop(c.key)
                flight.channels = [x for x in flight.channels if x.key != c.key]
        self.buffer = None
        self._msg(f"Recording stopped - {flight.duration:.1f} s, {flight.samples} samples")
        self.listener.on_recorded(flight)
        self._set_state(IDLE)

    # ======================================================================
    # Replay
    # ======================================================================
    def _start_replay(self, flight: Flight, start: float = 0.0, paused: bool = False) -> None:
        if self.state == RECORDING:
            self._stop_recording()
        if self.state == REPLAY:
            self._stop_replay(unfreeze=False)
        if self.state != IDLE:
            self._msg("Connect to the simulator first", "warning")
            return
        if flight.samples < 2:
            self._msg("This flight has no data", "warning")
            return
        self.flight = flight
        self.replay_t = max(0.0, min(start, flight.duration))
        self.replay_playing = not paused
        self._rebase_clock()
        self._reset_replay_state()
        self._set_state(REPLAY)
        self._plan_replay()
        if self.options.position:
            self._freeze(True)
        if not self._replay_plan["controls"]:
            self._neutral_controls()
        self._replay_update()
        plan = self._replay_plan
        self._msg(f"Replay - profile: {plan['profile'].name}; throttle: {plan['method']}; "
                  f"spool: {plan['spool']}")

    def _stop_replay(self, unfreeze: bool = True) -> None:
        if self.state != REPLAY:
            return
        if self.backend:
            if unfreeze:
                self._freeze(False)
            self._neutral_controls()
            if self._frame_subscribed:
                self.backend.unsubscribe_system_event(SYS_FRAME)
                self._frame_subscribed = False
        self.replay_playing = False
        self._set_state(IDLE)
        if self.flight is not None:
            self.listener.on_replay_position(self.replay_t, self.flight.duration, False)
        self._msg("Replay stopped")

    def _pause_replay(self, pause: bool) -> None:
        if self.state != REPLAY:
            return
        if pause and self.replay_playing:
            self.replay_t = self._replay_time()
            self.replay_playing = False
        elif not pause and not self.replay_playing:
            if self.flight and self.replay_t >= self.flight.duration:
                self.replay_t = 0.0
            self.replay_playing = True
            self._rebase_clock()

    def _seek(self, t: float) -> None:
        if self.flight is None:
            return
        self.replay_t = max(0.0, min(t, self.flight.duration))
        self._rebase_clock()
        self._reset_replay_state()
        if self.state == REPLAY:
            self._replay_update()

    def _rebase_clock(self) -> None:
        self._replay_wall0 = self.clock()
        self._replay_t0 = self.replay_t

    def _replay_time(self) -> float:
        if not self.replay_playing:
            return self.replay_t
        return self._replay_t0 + (self.clock() - self._replay_wall0) * self.replay_speed

    def _reset_replay_state(self) -> None:
        self._sent.clear()
        self._cooldown.clear()
        self._spoiler_rearm_at = None

    def _neutral_controls(self) -> None:
        """Centre the control axes FlyRoll may have moved (otherwise the last
        injected stick input stays applied after the replay)."""
        for event in ("AXIS_AILERONS_SET", "AXIS_ELEVATOR_SET", "AXIS_RUDDER_SET"):
            self._send(event, 0)
            self._sent[event] = 0

    def _freeze(self, on: bool) -> None:
        for name in ("FREEZE_LATITUDE_LONGITUDE_SET", "FREEZE_ALTITUDE_SET", "FREEZE_ATTITUDE_SET"):
            self._send(name, 1 if on else 0)

    # ---------------------------------------------------------------------
    def _plan_replay(self) -> None:
        """Work out, once per replay, which mechanisms drive the aircraft."""
        f, o = self.flight, self.options
        # The aircraft loaded *now* decides how it must be driven; fall back to the
        # profile the flight was recorded with when the current one is generic.
        if self.profile_override or self.profile.id != "default":
            profile = self.profile
        else:
            profile = self.profiles.get(f.meta.get("profile_id", "default"))
        n_eng = int(f.meta.get("engines") or 0) or int(profile.engines or 0) or 2
        n_eng = max(1, min(MAX_ENGINES, n_eng))
        eng_type = int(f.meta.get("engine_type", ENGINE_TYPE_JET))

        method = profile.throttle_method if o.throttle_method == "profile" else o.throttle_method
        rec_lvars = [n for n in profile.throttle_lvars if f.has("lvar." + n)]
        has_axis = any(f.has(f"axis.throttle.{i}") for i in range(1, n_eng + 1))
        if method == "auto":
            method = "lvar" if rec_lvars else ("axis" if has_axis else "simvar")
        if method == "lvar" and not rec_lvars:
            self._msg("Throttle method 'lvar' selected but this flight has no thrust-lever LVars - "
                      "falling back to axis/simvar", "warning")
            method = "axis" if has_axis else "simvar"
        spool = profile.spool if o.spool == "profile" else o.spool
        autostart = profile.engine_autostart if o.engine_autostart == "profile" else o.engine_autostart == "on"
        write_simvar = method == "simvar" or (profile.write_throttle_simvar and method == "axis")

        # spool variables by engine type
        if eng_type == ENGINE_TYPE_PISTON:
            spool_vars = ["rpm", "prop_rpm"]
            spool_ref = "rpm"
        elif eng_type == ENGINE_TYPE_TURBOPROP:
            spool_vars = ["n1", "n2", "prop_rpm"]
            spool_ref = "n1"
        else:
            spool_vars = ["n1", "n2"]
            spool_ref = "n1"

        b = self.backend
        # (re)build write definitions
        lever_keys = []
        for i in range(1, n_eng + 1):
            for v in ("throttle", "prop", "mixture"):
                if f.has(engine_key(v, i)):
                    lever_keys.append(engine_key(v, i))
        spool_keys = [engine_key(v, i) for i in range(1, n_eng + 1) for v in spool_vars if f.has(engine_key(v, i))]
        lvar_specs = []
        for spec in profile.all_lvars():
            if spec.replay == "off" or not f.has("lvar." + spec.name):
                continue
            if spec.name in profile.throttle_lvars and method != "lvar":
                continue
            lvar_specs.append(spec)
        lvar_keys = ["lvar." + s.name for s in lvar_specs]

        pose_keys = list(POSE_KEYS)
        if o.body_velocity != "off":
            pose_keys += VELOCITY_KEYS
        if o.rotation_rates:
            pose_keys += ROTATION_KEYS
        pose_keys = [k for k in pose_keys if f.has(k)]
        flex_keys = [k for k in ("flex.l", "flex.r") if f.has(k)] if o.wing_flex == "recorded" else []
        # body velocities: smoothed so rollout / reverser jitter doesn't become acceleration spikes
        overrides = smoothed(f, VELOCITY_KEYS + ROTATION_KEYS, 0.6) if o.body_velocity == "smoothed" else None
        self.sampler = Sampler(f, overrides)
        for did, keys in ((DEF_POSE, pose_keys), (DEF_LEVERS, lever_keys), (DEF_SPOOL, spool_keys),
                          (DEF_LVAR_WRITE, lvar_keys), (DEF_WINGFLEX, flex_keys)):
            b.clear_definition(did)
            if keys:
                b.add_definition(did, [f.channel(k).field() for k in keys])

        self._replay_plan = dict(profile=profile, engines=n_eng, method=method, spool=spool,
                                 autostart=autostart, write_simvar=write_simvar, spool_ref=spool_ref,
                                 pose_keys=pose_keys, lever_keys=lever_keys, spool_keys=spool_keys,
                                 lvar_keys=lvar_keys, has_axis=has_axis, flex_keys=flex_keys,
                                 controls=o.controls and profile.replay_controls)

    def _replay_tick_fallback(self) -> None:
        # Normally the replay advances on every simulator frame; if frames stop
        # arriving (e.g. Frame subscription not available) we tick at ~60 Hz.
        if self.state == REPLAY and self.clock() - self._last_replay_update > 1 / 30:
            self._replay_update()

    def _ensure_frame_subscription(self) -> None:
        if not self._frame_subscribed:
            self.backend.subscribe_system_event(SYS_FRAME, "Frame")
            self._frame_subscribed = True

    def _replay_update(self) -> None:
        if self.state != REPLAY or self.flight is None:
            return
        self._ensure_frame_subscription()
        now = self.clock()
        self._last_replay_update = now
        f, s, o, plan = self.flight, self.sampler, self.options, self._replay_plan
        t = self._replay_time()
        if t >= f.duration:
            if o.loop and self.replay_playing:
                self._seek(0.0)
                return
            t = f.duration
            if self.replay_playing:
                self.replay_t = t
                self.replay_playing = False
                self.listener.on_replay_position(t, f.duration, False)
        idx = s.index(t)
        v = lambda key: s.value(key, t, idx)  # noqa: E731
        live = self.live
        b = self.backend

        # 1. position & attitude
        if o.position and plan["pose_keys"]:
            self._set_data(DEF_POSE, [v(k) for k in plan["pose_keys"]], "pose")
        if plan["flex_keys"] and "wingflex" not in self._failed_tags:
            vals = [v(k) for k in plan["flex_keys"]]
            if all(math.isfinite(x) for x in vals):
                self._set_data(DEF_WINGFLEX, vals, "wingflex")

        profile: Profile = plan["profile"]
        # 2. primary controls (sign inverted like Sky Dolly)
        if plan["controls"]:
            for key, event in (("ctl.aileron", "AXIS_AILERONS_SET"), ("ctl.elevator", "AXIS_ELEVATOR_SET"),
                               ("ctl.rudder", "AXIS_RUDDER_SET")):
                val = v(key)
                if math.isfinite(val):
                    self._send_if_changed(event, int(-val * AXIS_MAX), 2)

        # 3. flaps (incremental like Sky Dolly, compared against the live handle)
        if o.flaps and profile.replay_flaps:
            want = v("sec.flaps_index")
            have = live.get("sec.flaps_index")
            if math.isfinite(want) and have is not None and int(round(want)) != int(round(have)) \
                    and self._ready("flaps", 0.5):
                diff = int(round(want)) - int(round(have))
                for _ in range(abs(diff)):
                    self._send("FLAPS_INCR" if diff > 0 else "FLAPS_DECR")

        # 4. spoilers
        if o.spoilers and profile.replay_spoilers:
            self._replay_spoilers(v)

        # 5. gear & parking brake (stateful: compare with live state)
        if o.gear and profile.replay_gear:
            self._sync_bool("hdl.gear", v("hdl.gear"), lambda on: self._send("GEAR_DOWN" if on else "GEAR_UP"))
            self._sync_bool("hdl.parking_brake", v("hdl.parking_brake"),
                            lambda on: self._send("PARKING_BRAKE_SET", 1 if on else 0))

        # 6. lights (test & toggle, each light mapped to ITS OWN state)
        if o.lights and profile.replay_lights:
            for key, _simvar, toggle in LIGHT_DEFS:
                self._sync_bool(key, v(key), lambda _on, ev=toggle: self._send(ev), cooldown=1.0)

        # 7. engines
        if o.engines:
            self._replay_engines(t, v, plan)


    def _replay_spoilers(self, v) -> None:
        """Speed-brake handle, arming, and retraction of auto-deployed ground spoilers.

        Airbus ground spoilers deploy by themselves (armed + reverse / touchdown)
        without the handle moving, so after scrubbing from the landing roll back
        into the air they would stay out.  When the recording shows retracted
        spoilers but both live surfaces are deployed (or the live handle is out),
        FlyRoll disarms, retracts, and re-arms a moment later if the recording
        was armed - the same sequence a pilot uses to reset them.
        """
        live = self.live
        now = self.clock()
        armed = v("sec.spoilers_armed")
        handle = v("sec.spoilers")
        if not (math.isfinite(armed) and math.isfinite(handle)):
            return
        rec_surface = max(v("sec.spoiler_l"), v("sec.spoiler_r"))
        live_surface = min(live.get("sec.spoiler_l", 0.0), live.get("sec.spoiler_r", 0.0))
        live_handle = live.get("sec.spoilers", 0.0)
        rec_retracted = handle <= 2.0 and (not math.isfinite(rec_surface) or rec_surface <= 5.0)
        if rec_retracted and (live_surface > 15.0 or live_handle > 10.0) and self._ready("spoiler_reset", 2.0):
            self._send("SPOILERS_ARM_SET", 0)
            self._send("SPOILERS_OFF")
            self._send("AXIS_SPOILER_SET", -16384)
            self._send("SPOILERS_SET", 0)
            self._sent["SPOILERS_ARM_SET"] = 0
            self._sent["SPOILERS_SET"] = 0
            self._spoiler_rearm_at = now + 1.0 if armed > 0.5 else None
            return
        if self._spoiler_rearm_at is not None:
            if now < self._spoiler_rearm_at:
                return
            self._spoiler_rearm_at = None
            self._sent.pop("SPOILERS_ARM_SET", None)
        self._send_if_changed("SPOILERS_ARM_SET", int(round(armed)), 0)
        raw = int(max(0.0, min(100.0, handle)) / 100.0 * AXIS_MAX)
        prev = self._sent.get("SPOILERS_SET")
        self._send_if_changed("SPOILERS_SET", raw, 100)
        if prev != self._sent.get("SPOILERS_SET"):
            if handle >= 98.0:
                self._send("SPOILERS_ON")
            elif handle <= 2.0 and armed < 0.5:
                self._send("SPOILERS_OFF")

    def _replay_engines(self, t: float, v, plan: dict) -> None:
        b = self.backend
        n_eng = plan["engines"]
        # levers via simvar (default aircraft / fallback)
        if plan["write_simvar"] and plan["lever_keys"]:
            vals = [v(k) for k in plan["lever_keys"]]
            if self._changed("_levers", vals, 0.2):
                self._set_data(DEF_LEVERS, vals, "levers")
        # levers via the aircraft's own LVars (+ any extra replayable LVars)
        if plan["lvar_keys"]:
            vals = [v(k) for k in plan["lvar_keys"]]
            if self._changed("_lvars", vals, 1e-4) or self._ready("_lvars_refresh", 0.5):
                self._set_data(DEF_LVAR_WRITE, vals, "lvars")
        # levers via the recorded hardware axis input
        if plan["method"] == "axis":
            for i in range(1, n_eng + 1):
                ax = v(f"axis.throttle.{i}")
                if math.isfinite(ax):
                    self._send_if_changed(f"THROTTLE{i}_AXIS_SET_EX1", int(ax), 0, refresh=1.0)
                else:
                    lever = v(engine_key("throttle", i))
                    if math.isfinite(lever):
                        raw = int(max(0.0, min(100.0, lever)) / 100.0 * AXIS_MAX)
                        self._send_if_changed(f"THROTTLE{i}_SET", raw, 40, refresh=1.0)
        # spool: N1/N2 (turbines) or RPM (pistons)
        spool = plan["spool"]
        if spool != "off" and plan["spool_keys"]:
            vals = [v(k) for k in plan["spool_keys"]]
            write = spool == "force"
            if not write:
                ref, tol = plan["spool_ref"], plan["profile"].spool_tolerance
                for i in range(1, n_eng + 1):
                    want = v(engine_key(ref, i))
                    have = self.live.get(engine_key(ref, i))
                    if not (math.isfinite(want) and have is not None):
                        continue
                    if ref == "rpm":
                        drift = abs(want - have) / max(want, 100.0) * 100.0
                    else:
                        drift = abs(want - have)
                    if drift > tol:
                        write = True
                        break
            if write and all(math.isfinite(x) for x in vals):
                self._set_data(DEF_SPOOL, vals, "spool")
        # engine start / shutdown events (default aircraft)
        if plan["autostart"]:
            want = any(v(engine_key("combustion", i)) > 0.5 for i in range(1, n_eng + 1))
            have = any(self.live.get(engine_key("combustion", i), 0.0) > 0.5 for i in range(1, n_eng + 1))
            if want != have and self._ready("engine_start", 10.0):
                self._send("ENGINE_AUTO_START" if want else "ENGINE_AUTO_SHUTDOWN")

    # ---------------------------------------------------------------------
    def _ready(self, key: str, cooldown: float) -> bool:
        now = self.clock()
        if now - self._cooldown.get(key, -1e9) >= cooldown:
            self._cooldown[key] = now
            return True
        return False

    def _changed(self, key: str, vals: List[float], eps: float) -> bool:
        prev = self._sent.get(key)
        if prev is None or len(prev) != len(vals) or any(abs(a - b) > eps for a, b in zip(prev, vals)):
            self._sent[key] = list(vals)
            return True
        return False

    def _send_if_changed(self, event: str, value: int, eps: int, refresh: float = 0.0) -> None:
        prev = self._sent.get(event)
        stale = refresh > 0 and self.clock() - self._cooldown.get("t:" + event, -1e9) > refresh
        if prev is None or abs(prev - value) > eps or stale:
            self._sent[event] = value
            self._cooldown["t:" + event] = self.clock()
            self._send(event, value)

    def _sync_bool(self, key: str, want: float, apply, cooldown: float = 1.0) -> None:
        if not math.isfinite(want):
            return
        have = self.live.get(key)
        if have is None:
            return
        want_b, have_b = want > 0.5, have > 0.5
        if want_b != have_b and self._ready("sync:" + key, cooldown):
            apply(want_b)

    # ======================================================================
    # LVar tester
    # ======================================================================
    def _set_probe(self, names: List[str]) -> None:
        clean = []
        for n in names:
            n = n.strip()
            if not n:
                continue
            if not n.upper().startswith(("L:", "Z:")):
                n = "L:" + n
            if n not in clean:
                clean.append(n)
        self.probe_names = clean
        if not self.backend:
            return
        b = self.backend
        b.request_data(DEF_PROBE, DEF_PROBE, PERIOD_NEVER)
        b.clear_definition(DEF_PROBE)
        if clean:
            b.add_definition(DEF_PROBE, [Field(n, "number") for n in clean])
            b.request_data(DEF_PROBE, DEF_PROBE, PERIOD_SIM_FRAME)

    def _write_lvar(self, name: str, value: float) -> None:
        if not self.backend:
            return
        if not name.upper().startswith(("L:", "Z:")):
            name = "L:" + name
        self.backend.clear_definition(DEF_PROBE_WRITE)
        self.backend.add_definition(DEF_PROBE_WRITE, [Field(name, "number")])
        self.backend.set_data(DEF_PROBE_WRITE, [float(value)])

    # ======================================================================
    def _set_state(self, state: str) -> None:
        self.state = state
        self.listener.on_state(state)

    def _msg(self, text: str, level: str = "info") -> None:
        self.listener.on_message(text, level)
