"""Backend abstraction over SimConnect.

Two implementations exist:
  * :class:`flyroll.sim.native.NativeBackend` - the real thing, talks to
    SimConnect.dll via ctypes (Windows only).
  * :class:`flyroll.sim.mock.MockBackend` - an in-process simulated aircraft
    used for the demo mode and the automated tests.

Every message coming back from the simulator is converted into one of the
small, plain Python message classes below, so the session logic never touches
raw SimConnect structures.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

# ---------------------------------------------------------------------------
# SimConnect constants (values from SimConnect.h, identical for 2020 and 2024)
# ---------------------------------------------------------------------------
OBJECT_ID_USER = 0
UNUSED = 0xFFFFFFFF

PERIOD_NEVER = 0
PERIOD_ONCE = 1
PERIOD_VISUAL_FRAME = 2
PERIOD_SIM_FRAME = 3
PERIOD_SECOND = 4

DATA_REQUEST_FLAG_DEFAULT = 0
DATA_REQUEST_FLAG_CHANGED = 1

DATATYPE_INT32 = 1
DATATYPE_FLOAT64 = 4
DATATYPE_STRING256 = 9

GROUP_PRIORITY_HIGHEST = 1
EVENT_FLAG_GROUPID_IS_PRIORITY = 0x10

# Field kinds used by FlyRoll data definitions
F64 = "f64"
STR256 = "str256"


@dataclass(frozen=True)
class Field:
    """One entry of a data definition (one simvar / LVar)."""

    name: str
    unit: str = "number"
    kind: str = F64


# ---------------------------------------------------------------------------
# Messages coming back from the simulator
# ---------------------------------------------------------------------------
@dataclass
class MsgOpen:
    app_name: str = ""
    app_version: str = ""


@dataclass
class MsgQuit:
    pass


@dataclass
class MsgException:
    code: int
    send_id: int
    index: int


@dataclass
class MsgData:
    request_id: int
    define_id: int
    payload: bytes


@dataclass
class MsgEvent:
    event_id: int
    data: int  # already converted to a signed 32 bit integer


@dataclass
class MsgFrame:
    event_id: int
    frame_rate: float
    sim_speed: float


@dataclass
class MsgFilename:
    event_id: int
    filename: str


@dataclass
class MsgSystemState:
    request_id: int
    integer: int
    number: float
    text: str


class Backend:
    """Interface implemented by the native and the mock backend."""

    name = "abstract"

    def open(self, app_name: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    @property
    def is_open(self) -> bool:
        raise NotImplementedError

    def add_definition(self, define_id: int, fields: Sequence[Field]) -> None:
        raise NotImplementedError

    def clear_definition(self, define_id: int) -> None:
        raise NotImplementedError

    def request_data(self, request_id: int, define_id: int, period: int,
                     flags: int = DATA_REQUEST_FLAG_DEFAULT) -> None:
        raise NotImplementedError

    def set_data(self, define_id: int, values: Sequence[float]) -> None:
        raise NotImplementedError

    def map_event(self, event_id: int, name: str) -> None:
        raise NotImplementedError

    def last_send_id(self) -> int:
        """Packet id of the last call (matches MsgException.send_id)."""
        return 0

    def transmit(self, event_id: int, data: int = 0) -> None:
        raise NotImplementedError

    def listen_event(self, group_id: int, event_id: int) -> None:
        """Receive notifications whenever ``event_id`` is fired (e.g. by joystick axes)."""
        raise NotImplementedError

    def subscribe_system_event(self, event_id: int, name: str) -> None:
        raise NotImplementedError

    def unsubscribe_system_event(self, event_id: int) -> None:
        raise NotImplementedError

    def request_system_state(self, request_id: int, state: str) -> None:
        raise NotImplementedError

    def wait(self, timeout_s: float) -> None:
        """Block until messages may be available (or the timeout expires)."""
        raise NotImplementedError

    def poll(self) -> Iterable[object]:
        """Return all messages currently queued by the simulator."""
        raise NotImplementedError


def to_int32(value: int) -> int:
    """Interpret an unsigned DWORD as signed 32 bit integer."""
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def to_dword(value: int) -> int:
    """Convert a (possibly negative) integer into an unsigned DWORD."""
    return int(value) & 0xFFFFFFFF


def payload_size(fields: Sequence[Field]) -> int:
    return sum(8 if f.kind == F64 else 256 for f in fields)
