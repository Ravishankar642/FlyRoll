"""Catalogue of everything FlyRoll records.

Each :class:`Channel` is one recorded value (one column of a flight).  The
``interp`` field decides how the value is interpolated during replay and the
``group`` decides which replay mechanism applies it to the aircraft.

Compared with Sky Dolly the engine group is much larger: Sky Dolly only stores
the *lever* positions (throttle / propeller / mixture), starter and combustion
flags - no N1, N2, RPM, prop RPM, EGT, ITT, fuel flow or reverser state - so a
replay can never show the engines spooling.  FlyRoll records all of them per
engine (up to 4 engines, i.e. A340 / A380 / 747 as well).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..sim.backend import Field

MAX_ENGINES = 4

# interpolation kinds
HERMITE = "hermite"          # cubic, smooth (positions)
HERMITE180 = "hermite180"    # cubic, wraps at +-180 (longitude)
ANGLE360 = "angle360"        # linear, wraps at 0/360 (heading)
ANGLE180 = "angle180"        # linear, wraps at +-180 (bank)
LINEAR = "linear"
STEP = "step"                # discrete (switches, indices)


@dataclass(frozen=True)
class Channel:
    key: str                 # unique column name, e.g. "eng.n1.2"
    simvar: Optional[str]    # simulation variable or "L:..." LVar; None for event-captured channels
    unit: str
    interp: str
    group: str
    label: str = ""
    precise: bool = False    # store as float64 (otherwise float32)
    settable: bool = False   # can be written back with SetDataOnSimObject

    def field(self) -> Field:
        assert self.simvar is not None
        return Field(self.simvar, self.unit)


def _c(key, simvar, unit, interp, group, label="", precise=False, settable=False) -> Channel:
    return Channel(key, simvar, unit, interp, group, label or key, precise, settable)


# --- position & attitude ------------------------------------------------------
POSITION: List[Channel] = [
    _c("pos.lat", "PLANE LATITUDE", "degrees", HERMITE, "position", "Latitude", True, True),
    _c("pos.lon", "PLANE LONGITUDE", "degrees", HERMITE180, "position", "Longitude", True, True),
    _c("pos.alt", "PLANE ALTITUDE", "feet", HERMITE, "position", "Altitude", True, True),
    _c("att.pitch", "PLANE PITCH DEGREES", "degrees", LINEAR, "position", "Pitch", True, True),
    _c("att.bank", "PLANE BANK DEGREES", "degrees", ANGLE180, "position", "Bank", True, True),
    _c("att.hdg", "PLANE HEADING DEGREES TRUE", "degrees", ANGLE360, "position", "Heading (true)", True, True),
    _c("vel.x", "VELOCITY BODY X", "feet per second", LINEAR, "position", settable=True),
    _c("vel.y", "VELOCITY BODY Y", "feet per second", LINEAR, "position", settable=True),
    _c("vel.z", "VELOCITY BODY Z", "feet per second", LINEAR, "position", settable=True),
    _c("rot.x", "ROTATION VELOCITY BODY X", "radians per second", LINEAR, "position", settable=True),
    _c("rot.y", "ROTATION VELOCITY BODY Y", "radians per second", LINEAR, "position", settable=True),
    _c("rot.z", "ROTATION VELOCITY BODY Z", "radians per second", LINEAR, "position", settable=True),
]

INFO: List[Channel] = [
    _c("info.ias", "AIRSPEED INDICATED", "knots", LINEAR, "info", "IAS"),
    _c("info.gs", "GROUND VELOCITY", "knots", LINEAR, "info", "Ground speed"),
    _c("info.vs", "VERTICAL SPEED", "feet per minute", LINEAR, "info", "Vertical speed"),
    _c("info.agl", "PLANE ALT ABOVE GROUND", "feet", LINEAR, "info", "AGL"),
    _c("info.on_ground", "SIM ON GROUND", "bool", STEP, "info", "On ground"),
]

# --- primary flight controls (replayed via AXIS_*_SET like Sky Dolly) ----------
PRIMARY: List[Channel] = [
    _c("ctl.aileron", "AILERON POSITION", "position", LINEAR, "primary", "Aileron"),
    _c("ctl.elevator", "ELEVATOR POSITION", "position", LINEAR, "primary", "Elevator"),
    _c("ctl.rudder", "RUDDER POSITION", "position", LINEAR, "primary", "Rudder"),
]

# --- secondary flight controls & handles ---------------------------------------
SECONDARY: List[Channel] = [
    _c("sec.flaps_index", "FLAPS HANDLE INDEX", "number", STEP, "secondary", "Flaps handle"),
    _c("sec.flaps_l", "TRAILING EDGE FLAPS LEFT PERCENT", "percent", LINEAR, "secondary"),
    _c("sec.flaps_r", "TRAILING EDGE FLAPS RIGHT PERCENT", "percent", LINEAR, "secondary"),
    _c("sec.slats_l", "LEADING EDGE FLAPS LEFT PERCENT", "percent", LINEAR, "secondary"),
    _c("sec.slats_r", "LEADING EDGE FLAPS RIGHT PERCENT", "percent", LINEAR, "secondary"),
    _c("sec.spoilers", "SPOILERS HANDLE POSITION", "percent", LINEAR, "secondary", "Spoilers"),
    _c("sec.spoilers_armed", "SPOILERS ARMED", "bool", STEP, "secondary", "Spoilers armed"),
    _c("sec.spoiler_l", "SPOILERS LEFT POSITION", "percent", LINEAR, "secondary", "Spoiler surface L"),
    _c("sec.spoiler_r", "SPOILERS RIGHT POSITION", "percent", LINEAR, "secondary", "Spoiler surface R"),
]

# --- structure: wing flex is normally computed by the flight model from the
#     accelerations; during replay those are artificial, so FlyRoll can write
#     the recorded flex back instead.
STRUCTURE: List[Channel] = [
    _c("flex.l", "WING FLEX PCT:1", "percent over 100", LINEAR, "structure", "Wing flex L", settable=True),
    _c("flex.r", "WING FLEX PCT:2", "percent over 100", LINEAR, "structure", "Wing flex R", settable=True),
    _c("info.g", "G FORCE", "gforce", LINEAR, "info", "G force"),
]

HANDLES: List[Channel] = [
    _c("hdl.gear", "GEAR HANDLE POSITION", "bool", STEP, "handles", "Gear handle"),
    _c("hdl.parking_brake", "BRAKE PARKING POSITION", "bool", STEP, "handles", "Parking brake"),
    _c("hdl.tailhook", "TAILHOOK HANDLE", "bool", STEP, "handles"),
    _c("hdl.wingfold", "FOLDING WING HANDLE POSITION", "bool", STEP, "handles"),
    _c("hdl.steer", "GEAR STEER ANGLE PCT", "percent over 100", LINEAR, "handles"),
    _c("hdl.canopy", "CANOPY OPEN", "percent", LINEAR, "handles", settable=True),
]

# --- lights: (key, simvar, toggle event) -------------------------------------
LIGHT_DEFS = [
    ("light.nav", "LIGHT NAV", "TOGGLE_NAV_LIGHTS"),
    ("light.beacon", "LIGHT BEACON", "TOGGLE_BEACON_LIGHTS"),
    ("light.landing", "LIGHT LANDING", "LANDING_LIGHTS_TOGGLE"),
    ("light.taxi", "LIGHT TAXI", "TOGGLE_TAXI_LIGHTS"),
    ("light.strobe", "LIGHT STROBE", "STROBES_TOGGLE"),
    ("light.panel", "LIGHT PANEL", "PANEL_LIGHTS_TOGGLE"),
    ("light.recognition", "LIGHT RECOGNITION", "TOGGLE_RECOGNITION_LIGHTS"),
    ("light.wing", "LIGHT WING", "TOGGLE_WING_LIGHTS"),
    ("light.logo", "LIGHT LOGO", "TOGGLE_LOGO_LIGHTS"),
    ("light.cabin", "LIGHT CABIN", "TOGGLE_CABIN_LIGHTS"),
]
LIGHTS: List[Channel] = [_c(k, v, "bool", STEP, "lights", v.title()) for k, v, _ in LIGHT_DEFS]

# --- engines -------------------------------------------------------------------
# (suffix, simvar, unit, interp, settable)
ENGINE_VARS = [
    ("throttle", "GENERAL ENG THROTTLE LEVER POSITION", "percent", LINEAR, True),
    ("prop", "GENERAL ENG PROPELLER LEVER POSITION", "percent", LINEAR, True),
    ("mixture", "GENERAL ENG MIXTURE LEVER POSITION", "percent", LINEAR, True),
    ("n1", "TURB ENG N1", "percent", LINEAR, True),
    ("n2", "TURB ENG N2", "percent", LINEAR, True),
    ("cn1", "TURB ENG CORRECTED N1", "percent", LINEAR, True),
    ("rpm", "GENERAL ENG RPM", "rpm", LINEAR, True),
    ("prop_rpm", "PROP RPM", "rpm", LINEAR, True),
    ("egt", "GENERAL ENG EXHAUST GAS TEMPERATURE", "celsius", LINEAR, True),
    ("itt", "TURB ENG ITT", "celsius", LINEAR, True),
    ("ff", "TURB ENG FUEL FLOW PPH", "pounds per hour", LINEAR, False),
    ("ff_gph", "ENG FUEL FLOW GPH", "gallons per hour", LINEAR, False),
    ("reverser", "TURB ENG REVERSE NOZZLE PERCENT", "percent", LINEAR, False),
    ("combustion", "GENERAL ENG COMBUSTION", "bool", STEP, False),
    ("starter", "GENERAL ENG STARTER", "bool", STEP, False),
    ("cowl", "RECIP ENG COWL FLAP POSITION", "percent", LINEAR, True),
]
ENGINE_LABELS = {
    "throttle": "Throttle lever", "prop": "Prop lever", "mixture": "Mixture", "n1": "N1", "n2": "N2",
    "cn1": "Corrected N1", "rpm": "RPM", "prop_rpm": "Prop RPM", "egt": "EGT", "itt": "ITT",
    "ff": "Fuel flow", "ff_gph": "Fuel flow (gph)", "reverser": "Reverser", "combustion": "Combustion",
    "starter": "Starter", "cowl": "Cowl flaps",
}


def engine_key(var: str, engine: int) -> str:
    return f"eng.{var}.{engine}"


ENGINES: List[Channel] = [
    _c(engine_key(suffix, i), f"{simvar}:{i}", unit, interp, "engine",
       f"{ENGINE_LABELS[suffix]} {i}", settable=settable)
    for i in range(1, MAX_ENGINES + 1)
    for suffix, simvar, unit, interp, settable in ENGINE_VARS
]

AIRCRAFT_META: List[Channel] = [
    _c("meta.engines", "NUMBER OF ENGINES", "number", STEP, "meta"),
    _c("meta.engine_type", "ENGINE TYPE", "enum", STEP, "meta"),
]

# Raw throttle axis input captured from joystick / throttle quadrant events.
# These are what fixes replay for FADEC airliners: the aircraft's own
# throttle logic receives exactly what the hardware sent while recording.
AXIS: List[Channel] = [
    Channel(f"axis.throttle.{i}", None, "axis", STEP, "axis", f"Throttle axis {i}")
    for i in range(1, MAX_ENGINES + 1)
]

# Events whose value we observe while recording. value_kind:
#   "axis"   -> -16384..16383 already
#   "set"    -> 0..16383, converted to axis range
AXIS_INPUT_EVENTS = [
    *[(f"THROTTLE{i}_AXIS_SET_EX1", i, "axis") for i in range(1, 5)],
    ("THROTTLE_AXIS_SET_EX1", 0, "axis"),
    *[(f"AXIS_THROTTLE{i}_SET", i, "axis") for i in range(1, 5)],
    ("AXIS_THROTTLE_SET", 0, "axis"),
    *[(f"THROTTLE{i}_SET", i, "set") for i in range(1, 5)],
    ("THROTTLE_SET", 0, "set"),
]

# Engine-type enum of the ENGINE TYPE simvar
ENGINE_TYPE_PISTON = 0
ENGINE_TYPE_JET = 1
ENGINE_TYPE_NONE = 2
ENGINE_TYPE_HELO_TURBINE = 3
ENGINE_TYPE_TURBOPROP = 5


def lvar_key(name: str) -> str:
    return "lvar." + name


def lvar_channel(name: str, interp: str = LINEAR) -> Channel:
    """A recorded LVar. ``name`` must include the ``L:`` prefix."""
    return Channel(lvar_key(name), name, "number", interp, "lvar", name, settable=True)


def base_channels() -> List[Channel]:
    return [*POSITION, *INFO, *PRIMARY, *SECONDARY, *STRUCTURE, *HANDLES, *LIGHTS, *ENGINES, *AIRCRAFT_META]


POSE_KEYS = ["pos.lat", "pos.lon", "pos.alt", "att.pitch", "att.bank", "att.hdg"]
VELOCITY_KEYS = ["vel.x", "vel.y", "vel.z"]
ROTATION_KEYS = ["rot.x", "rot.y", "rot.z"]


def channel_map(channels: List[Channel]) -> Dict[str, Channel]:
    return {c.key: c for c in channels}
