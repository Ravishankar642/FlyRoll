"""End-to-end tests against the mock simulator (run: python -m pytest tests)."""
import math
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("FLYROLL_HOME", tempfile.mkdtemp())

from flyroll.core.flight import Sampler, hermite  # noqa: E402
from flyroll.core.logbook import Logbook  # noqa: E402
from flyroll.core.profiles import ProfileStore  # noqa: E402
from flyroll.core.session import IDLE, RECORDING, REPLAY, ReplayOptions, Session, SessionListener  # noqa: E402
from flyroll.sim.mock import MockBackend  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Collector(SessionListener):
    def __init__(self):
        self.flights = []
        self.messages = []
        self.aircraft = {}

    def on_recorded(self, flight):
        self.flights.append(flight)

    def on_message(self, text, level="info"):
        self.messages.append((level, text))

    def on_aircraft(self, info):
        self.aircraft = info


def make(kind, pilot=True):
    clock = Clock()
    holder = {}

    def factory():
        holder["b"] = MockBackend(kind, clock=clock, pilot=pilot, realtime=False)
        return holder["b"]

    lst = Collector()
    s = Session(factory, ProfileStore(), lst, clock=clock)
    s.step()  # connects
    run(s, clock, 0.5)
    return s, clock, holder["b"], lst


def run(session, clock, seconds, dt=1 / 60):
    for _ in range(int(seconds / dt)):
        clock.t += dt
        session.step()


def record(kind, seconds=60.0):
    s, clock, b, lst = make(kind)
    assert s.state == IDLE
    s.record()
    run(s, clock, seconds)
    assert s.state == RECORDING
    s.stop_record()
    s.step()
    assert lst.flights, lst.messages
    return lst.flights[0], lst


def test_profile_detection():
    for kind, pid in (("fenix", "fenix-a32x"), ("a350", "inibuilds-a350"), ("a380", "inibuilds-a380"),
                      ("c172", "default")):
        s, *_ = make(kind)
        assert s.profile.id == pid, (kind, s.profile.id)


def test_fenix_records_engine_and_lvars():
    flight, _ = record("fenix")
    assert flight.duration > 55
    assert flight.meta["profile_id"] == "fenix-a32x"
    n1 = flight.columns["eng.n1.1"]
    assert n1.min() < 25 and n1.max() > 80, "N1 spool-up must be recorded"
    lever = flight.columns["lvar.L:A_FC_THROTTLE_LEFT_INPUT"]
    assert lever.max() == pytest.approx(5.0, abs=0.01), "TOGA detent recorded from the Fenix LVar"
    assert flight.columns["light.strobe"].max() == 1 and flight.columns["light.taxi"].max() == 0


def replay_into(flight, kind, options=None, at=30.0, seconds=None):
    s, clock, b, lst = make(kind, pilot=False)
    if options:
        s.set_options(options)
    s.play(flight)
    run(s, clock, seconds if seconds is not None else at)
    assert s.state == REPLAY
    return s, b


def test_fenix_replay_moves_levers_and_spools_engines():
    flight, _ = record("fenix")
    s, b = replay_into(flight, "fenix", at=30.0)
    assert s._replay_plan["method"] == "lvar"
    sm = Sampler(flight)
    t = s._replay_time()
    assert b.ac.lvars["L:A_FC_THROTTLE_LEFT_INPUT"] == pytest.approx(sm.value("lvar.L:A_FC_THROTTLE_LEFT_INPUT", t), abs=0.05)
    assert b.ac.n1[1] == pytest.approx(sm.value("eng.n1.1", t), abs=3.0)
    assert b.ac.n1[1] > 80
    assert b.ac.frozen
    # lights: strobe ON, taxi stays OFF (Sky Dolly fed the taxi state into the strobe)
    assert b.ac.lights["STROBE"] == 1.0 and b.ac.lights["TAXI"] == 0.0
    assert b.ac.lat == pytest.approx(sm.value("pos.lat", t), abs=1e-4)


def test_sky_dolly_method_fails_on_fadec_aircraft():
    """Reproduces the original problem: writing the throttle simvar does nothing on a FADEC aircraft."""
    flight, _ = record("fenix")
    opts = ReplayOptions(throttle_method="simvar", spool="off")
    s, b = replay_into(flight, "fenix", opts, at=30.0)
    assert b.ac.writes_ignored > 0
    assert b.ac.n1[1] < 25, "engines stay at idle - this is what Sky Dolly shows"


def test_inibuilds_axis_replay():
    flight, _ = record("a350")
    assert flight.has("axis.throttle.1") and flight.has("axis.throttle.2")
    s, b = replay_into(flight, "a350", at=30.0)
    assert s._replay_plan["method"] == "axis"
    sent = [n for n, _ in b.transmitted if n.startswith("THROTTLE")]
    assert "THROTTLE1_AXIS_SET_EX1" in sent
    assert b.ac.lever[1] == pytest.approx(100.0, abs=1.0)
    assert b.ac.n1[1] > 80


def test_a380_four_engines():
    flight, _ = record("a380")
    assert flight.meta["engines"] == 4
    s, b = replay_into(flight, "a380", at=30.0)
    assert all(b.ac.n1[i] > 80 for i in range(1, 5))


def test_spool_assist_when_levers_differ():
    """Recorded with A/THR style N1 (levers don't explain it) -> assist writes N1."""
    flight, _ = record("fenix")
    opts = ReplayOptions(throttle_method="simvar", spool="assist")
    s, b = replay_into(flight, "fenix", opts, at=30.0)
    sm = Sampler(flight)
    assert b.ac.n1[1] == pytest.approx(sm.value("eng.n1.1", s._replay_time()), abs=3.0)


def test_default_aircraft_simvar_levers():
    flight, _ = record("c172")
    s, b = replay_into(flight, "c172", at=30.0)
    assert b.ac.lever[1] > 90


def test_logbook_roundtrip(tmp_path):
    flight, _ = record("fenix", 10)
    lb = Logbook(tmp_path / "logbook.sqlite")
    fid = lb.save(flight)
    again = lb.load(fid)
    assert again.samples == flight.samples
    np.testing.assert_allclose(again.columns["eng.n1.1"], flight.columns["eng.n1.1"])
    lb.export_file(fid, tmp_path / "x.flyroll")
    ids = lb.import_file(tmp_path / "x.flyroll")
    assert len(lb.list()) == 2 and ids
    Logbook.export_csv(again, tmp_path / "x.csv")
    assert (tmp_path / "x.csv").read_text().startswith("time_s,")


def test_hermite_and_wrap():
    assert hermite(0, 1, 2, 3, 0.5) == pytest.approx(1.5)
    from flyroll.core.flight import Flight
    from flyroll.core.channels import POSITION
    lon = next(c for c in POSITION if c.key == "pos.lon")
    hdg = next(c for c in POSITION if c.key == "att.hdg")
    f = Flight([lon, hdg], np.array([0.0, 1.0, 2.0, 3.0]),
               {"pos.lon": np.array([178.0, 179.0, -179.0, -178.0]),
                "att.hdg": np.array([350.0, 359.0, 9.0, 19.0])})
    sm = Sampler(f)
    assert abs(sm.value("pos.lon", 1.5)) == pytest.approx(180.0, abs=0.01)
    assert sm.value("att.hdg", 1.5) == pytest.approx(4.0, abs=0.01)


def test_native_parser():
    import struct
    from flyroll.sim.native import parse_message
    from flyroll.sim.backend import MsgData, MsgEvent, MsgFrame
    raw = struct.pack("<III", 48, 6, 8) + struct.pack("<7I", 5, 1, 2, 0, 1, 1, 1) + struct.pack("<d", 42.5)
    m = parse_message(raw)
    assert isinstance(m, MsgData) and m.request_id == 5 and struct.unpack("<d", m.payload)[0] == 42.5
    raw = struct.pack("<III", 24, 6, 4) + struct.pack("<III", 1, 107, 0xFFFFC000)
    m = parse_message(raw)
    assert isinstance(m, MsgEvent) and m.event_id == 107 and m.data == -16384
    raw = struct.pack("<III", 32, 6, 7) + struct.pack("<III", 0, 50, 0) + struct.pack("<ff", 60.0, 1.0)
    m = parse_message(raw)
    assert isinstance(m, MsgFrame) and m.frame_rate == 60.0


def test_wall_clock_fallback_when_sim_time_frozen(monkeypatch):
    from flyroll.sim import mock
    orig = mock.MockAircraft.get
    monkeypatch.setattr(mock.MockAircraft, "get",
                        lambda self, name: 0.0 if name == "SIMULATION TIME" else orig(self, name))
    flight, lst = record("fenix", 20)
    assert flight.duration > 15
    assert any("system clock" in m for _, m in lst.messages)


def test_pause_does_not_add_time():
    s, clock, b, lst = make("fenix")
    s.record()
    run(s, clock, 5)
    b.set_paused(True)
    run(s, clock, 5)
    b.set_paused(False)
    run(s, clock, 5)
    s.stop_record()
    s.step()
    assert lst.flights[0].duration == pytest.approx(10.0, abs=0.3)


def record_landing():
    """Fenix-like recording: climb, then arm spoilers and select reverse -> ground spoilers deploy."""
    s, clock, b, lst = make("fenix")
    s.record()
    run(s, clock, 20)
    b.ac.event("SPOILERS_ARM_SET", 1)
    run(s, clock, 5)
    for name in ("L:A_FC_THROTTLE_LEFT_INPUT", "L:A_FC_THROTTLE_RIGHT_INPUT"):
        b.ac.lvars[name] = 0.5          # reverse
    run(s, clock, 10)
    s.stop_record()
    s.step()
    return lst.flights[0]


def test_fbw_profile_does_not_inject_stick_and_recentres():
    flight, _ = record("fenix", 20)
    s, b = replay_into(flight, "fenix", at=5)
    sent = [(n, v) for n, v in b.transmitted if n.startswith("AXIS_AILERONS")]
    assert sent and all(v == 0 for _, v in sent), "only neutral aileron input for fly-by-wire aircraft"
    s.stop_replay()
    s.step()
    assert b.transmitted[-1][0] in ("AXIS_RUDDER_SET", "FREEZE_ATTITUDE_SET") or \
        ("AXIS_AILERONS_SET", 0) in b.transmitted[-6:]


def test_scrub_moves_aircraft_while_paused():
    flight, _ = record("fenix", 40)
    s, clock, b, lst = make("fenix", pilot=False)
    s.play(flight, 30.0, paused=True)
    run(s, clock, 0.2)
    sm = Sampler(flight)
    assert s.state == REPLAY and not s.replay_playing
    assert b.ac.lat == pytest.approx(sm.value("pos.lat", 30.0), abs=1e-6)
    for t in (25.0, 20.0, 10.0):      # a drag sends a burst of seeks
        s.seek(t + 0.5)
        s.seek(t)
        run(s, clock, 0.1)
        assert b.ac.lat == pytest.approx(sm.value("pos.lat", t), abs=1e-6)
        assert s._replay_time() == pytest.approx(t)


def test_ground_spoilers_reset_when_scrubbing_back_to_air():
    flight = record_landing()
    assert flight.columns["sec.spoiler_l"].max() > 90, "ground spoilers recorded"
    s, clock, b, lst = make("fenix", pilot=False)
    s.play(flight, flight.duration - 1, paused=True)
    run(s, clock, 3)
    assert b.ac.ground_spoilers, "rollout: armed + reverse -> ground spoilers out"
    s.seek(15.0)                      # back in the air, spoilers were not armed yet
    run(s, clock, 3)
    assert not b.ac.ground_spoilers and b.ac.spoilers == 0
    assert b.ac.spoilers_armed == 0
    s.seek(22.0)                      # approach: armed in the recording
    run(s, clock, 3)
    assert not b.ac.ground_spoilers and b.ac.spoilers_armed == 1


def test_wing_flex_written_and_falls_back():
    flight, _ = record("fenix", 30)
    assert flight.has("flex.l")
    s, b = replay_into(flight, "fenix", at=20)
    sm = Sampler(flight)
    assert b.ac.vars["WING FLEX PCT:1"] == pytest.approx(sm.value("flex.l", s._replay_time()), abs=0.01)
    # simulator refuses the write -> feature switches itself off, warns once
    s2, clock, b2, lst = make("fenix", pilot=False)
    b2.reject = {"WING FLEX PCT:1"}
    s2.play(flight)
    run(s2, clock, 2)
    warnings = [m for lvl, m in lst.messages if "Wing flex" in m]
    assert len(warnings) == 1 and "wingflex" in s2._failed_tags


def test_velocity_smoothing_option():
    flight, _ = record("fenix", 20)
    s, b = replay_into(flight, "fenix", ReplayOptions(body_velocity="off"), at=2)
    assert "vel.z" not in s._replay_plan["pose_keys"]
    s, b = replay_into(flight, "fenix", at=2)
    assert "vel.z" in s._replay_plan["pose_keys"] and "rot.x" not in s._replay_plan["pose_keys"]


def test_new_profiles_detected():
    store = ProfileStore()
    assert store.detect(r"SimObjects\Airplanes\fslabs-aircraft-a321\aircraft.cfg").id == "fslabs-a321"
    assert store.detect("iFly 737-MAX8").id == "ifly-737max"
    assert store.get("fslabs-a321").replay_controls is False
    assert store.get("ifly-737max").replay_controls is True
