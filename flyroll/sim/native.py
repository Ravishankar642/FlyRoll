"""Native SimConnect backend (Windows only) implemented with ctypes.

No compiled extension is needed: FlyRoll loads ``SimConnect.dll`` at runtime.
The DLL is looked up in this order:

1. the ``FLYROLL_SIMCONNECT_DLL`` environment variable (full path to the dll)
2. next to FlyRoll.exe / the PyInstaller bundle / this package
3. the MSFS 2024 SDK (``MSFS2024_SDK`` env var or ``C:\\MSFS 2024 SDK``)
4. the MSFS 2020 SDK (``MSFS_SDK`` env var or ``C:\\MSFS SDK``)
5. the ``SimConnect`` pip package (which ships a SimConnect.dll)
"""
from __future__ import annotations

import ctypes
import importlib.util
import os
import struct
import sys
from ctypes import POINTER, byref, c_char_p, c_float, c_uint32, c_void_p
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from .backend import (
    DATATYPE_FLOAT64,
    DATATYPE_STRING256,
    EVENT_FLAG_GROUPID_IS_PRIORITY,
    F64,
    GROUP_PRIORITY_HIGHEST,
    OBJECT_ID_USER,
    UNUSED,
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
    payload_size,
    to_dword,
    to_int32,
)

S_OK = 0

# SIMCONNECT_RECV_ID values
RECV_NULL = 0
RECV_EXCEPTION = 1
RECV_OPEN = 2
RECV_QUIT = 3
RECV_EVENT = 4
RECV_EVENT_FILENAME = 6
RECV_EVENT_FRAME = 7
RECV_SIMOBJECT_DATA = 8
RECV_SIMOBJECT_DATA_BYTYPE = 9
RECV_SYSTEM_STATE = 15

# Offsets inside the received structures (all fields are 4 byte DWORDs)
#   SIMCONNECT_RECV                : dwSize, dwVersion, dwID                    -> 12 bytes
#   SIMCONNECT_RECV_SIMOBJECT_DATA : + dwRequestID, dwObjectID, dwDefineID,
#                                      dwFlags, dwentrynumber, dwoutof,
#                                      dwDefineCount                            -> data @ 40
#   SIMCONNECT_RECV_EVENT          : + uGroupID, uEventID, dwData               -> 24 bytes
#   SIMCONNECT_RECV_EVENT_FRAME    : + fFrameRate, fSimSpeed (floats)           @ 24 / 28
#   SIMCONNECT_RECV_EVENT_FILENAME : + szFileName[MAX_PATH], dwFlags            @ 24
#   SIMCONNECT_RECV_SYSTEM_STATE   : + dwRequestID, dwInteger, fFloat, szString @ 12/16/20/24
#   SIMCONNECT_RECV_EXCEPTION      : + dwException, dwSendID, dwIndex           @ 12/16/20
HEADER = struct.Struct("<III")
DATA_OFFSET = 40
MAX_PATH = 260


class SimConnectError(RuntimeError):
    pass


class SimConnectNotFound(SimConnectError):
    pass


def candidate_dll_paths() -> List[Path]:
    paths: List[Path] = []
    env = os.environ.get("FLYROLL_SIMCONNECT_DLL")
    if env:
        paths.append(Path(env))
    roots = []
    if getattr(sys, "_MEIPASS", None):
        roots.append(Path(sys._MEIPASS))  # type: ignore[attr-defined]
    roots.append(Path(sys.executable).resolve().parent)
    roots.append(Path(__file__).resolve().parent)
    roots.append(Path(__file__).resolve().parents[2])
    paths.extend(r / "SimConnect.dll" for r in roots)
    for var, default in (("MSFS2024_SDK", r"C:\MSFS 2024 SDK"), ("MSFS_SDK", r"C:\MSFS SDK")):
        sdk = os.environ.get(var) or default
        paths.append(Path(sdk) / "SimConnect SDK" / "lib" / "SimConnect.dll")
    try:
        spec = importlib.util.find_spec("SimConnect")
        if spec and spec.origin:
            paths.append(Path(spec.origin).parent / "SimConnect.dll")
    except (ImportError, ValueError):
        pass
    return paths


def find_simconnect_dll() -> Path:
    for p in candidate_dll_paths():
        if p.is_file():
            return p
    raise SimConnectNotFound(
        "SimConnect.dll not found. Copy it next to FlyRoll.exe (from "
        "'MSFS 2024 SDK\\SimConnect SDK\\lib\\SimConnect.dll') or run "
        "'pip install SimConnect'. Searched:\n  " + "\n  ".join(str(p) for p in candidate_dll_paths())
    )


class NativeBackend(Backend):
    name = "SimConnect"

    def __init__(self, dll_path: Optional[Path] = None) -> None:
        if sys.platform != "win32":
            raise SimConnectError("The native SimConnect backend only works on Windows")
        self.dll_path = Path(dll_path) if dll_path else find_simconnect_dll()
        self._dll = ctypes.WinDLL(str(self.dll_path))
        self._k32 = ctypes.WinDLL("kernel32")
        self._handle = c_void_p()
        self._event = None
        self._bind()

    # -- ctypes signatures --------------------------------------------------
    def _bind(self) -> None:
        d = self._dll
        HANDLE = c_void_p
        DWORD = c_uint32
        d.SimConnect_Open.argtypes = [POINTER(HANDLE), c_char_p, c_void_p, DWORD, HANDLE, DWORD]
        d.SimConnect_Close.argtypes = [HANDLE]
        d.SimConnect_AddToDataDefinition.argtypes = [HANDLE, DWORD, c_char_p, c_char_p, DWORD, c_float, DWORD]
        d.SimConnect_ClearDataDefinition.argtypes = [HANDLE, DWORD]
        d.SimConnect_RequestDataOnSimObject.argtypes = [HANDLE, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD]
        d.SimConnect_SetDataOnSimObject.argtypes = [HANDLE, DWORD, DWORD, DWORD, DWORD, DWORD, c_void_p]
        d.SimConnect_MapClientEventToSimEvent.argtypes = [HANDLE, DWORD, c_char_p]
        d.SimConnect_TransmitClientEvent.argtypes = [HANDLE, DWORD, DWORD, DWORD, DWORD, DWORD]
        d.SimConnect_AddClientEventToNotificationGroup.argtypes = [HANDLE, DWORD, DWORD, ctypes.c_int]
        d.SimConnect_SetNotificationGroupPriority.argtypes = [HANDLE, DWORD, DWORD]
        d.SimConnect_SubscribeToSystemEvent.argtypes = [HANDLE, DWORD, c_char_p]
        d.SimConnect_UnsubscribeFromSystemEvent.argtypes = [HANDLE, DWORD]
        d.SimConnect_RequestSystemState.argtypes = [HANDLE, DWORD, c_char_p]
        d.SimConnect_GetNextDispatch.argtypes = [HANDLE, POINTER(c_void_p), POINTER(DWORD)]
        d.SimConnect_GetLastSentPacketID.argtypes = [HANDLE, POINTER(DWORD)]
        for fn in ("SimConnect_Open", "SimConnect_Close", "SimConnect_AddToDataDefinition",
                   "SimConnect_ClearDataDefinition", "SimConnect_RequestDataOnSimObject",
                   "SimConnect_SetDataOnSimObject", "SimConnect_MapClientEventToSimEvent",
                   "SimConnect_TransmitClientEvent", "SimConnect_AddClientEventToNotificationGroup",
                   "SimConnect_SetNotificationGroupPriority", "SimConnect_SubscribeToSystemEvent",
                   "SimConnect_UnsubscribeFromSystemEvent", "SimConnect_RequestSystemState",
                   "SimConnect_GetNextDispatch", "SimConnect_GetLastSentPacketID"):
            getattr(d, fn).restype = ctypes.c_long
        self._k32.CreateEventW.restype = c_void_p
        self._k32.CreateEventW.argtypes = [c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
        self._k32.WaitForSingleObject.argtypes = [c_void_p, DWORD]
        self._k32.CloseHandle.argtypes = [c_void_p]

    def _check(self, hr: int, what: str) -> None:
        if hr != S_OK:
            raise SimConnectError(f"{what} failed (HRESULT 0x{to_dword(hr):08X})")

    # -- Backend API ----------------------------------------------------------
    def open(self, app_name: str) -> None:
        self._event = self._k32.CreateEventW(None, 0, 0, None)
        hr = self._dll.SimConnect_Open(byref(self._handle), app_name.encode(), None, 0, self._event, 0)
        if hr != S_OK:
            self._handle = c_void_p()
            raise SimConnectError("Could not connect - is Microsoft Flight Simulator running?")

    def close(self) -> None:
        if self._handle:
            self._dll.SimConnect_Close(self._handle)
            self._handle = c_void_p()
        if self._event:
            self._k32.CloseHandle(self._event)
            self._event = None

    @property
    def is_open(self) -> bool:
        return bool(self._handle)

    def add_definition(self, define_id: int, fields: Sequence[Field]) -> None:
        for f in fields:
            dtype = DATATYPE_FLOAT64 if f.kind == F64 else DATATYPE_STRING256
            unit = f.unit.encode() if f.kind == F64 else None
            hr = self._dll.SimConnect_AddToDataDefinition(self._handle, define_id, f.name.encode(), unit,
                                                          dtype, 0.0, UNUSED)
            self._check(hr, f"AddToDataDefinition({f.name})")

    def clear_definition(self, define_id: int) -> None:
        self._dll.SimConnect_ClearDataDefinition(self._handle, define_id)

    def request_data(self, request_id: int, define_id: int, period: int, flags: int = 0) -> None:
        hr = self._dll.SimConnect_RequestDataOnSimObject(self._handle, request_id, define_id, OBJECT_ID_USER,
                                                         period, flags, 0, 0, 0)
        self._check(hr, "RequestDataOnSimObject")

    def set_data(self, define_id: int, values: Sequence[float]) -> None:
        buf = (ctypes.c_double * len(values))(*values)
        hr = self._dll.SimConnect_SetDataOnSimObject(self._handle, define_id, OBJECT_ID_USER, 0, 0,
                                                     ctypes.sizeof(buf), ctypes.cast(buf, c_void_p))
        self._check(hr, "SetDataOnSimObject")

    def last_send_id(self) -> int:
        sid = c_uint32()
        if self._handle and self._dll.SimConnect_GetLastSentPacketID(self._handle, byref(sid)) == S_OK:
            return int(sid.value)
        return 0

    def map_event(self, event_id: int, name: str) -> None:
        self._check(self._dll.SimConnect_MapClientEventToSimEvent(self._handle, event_id, name.encode()),
                    f"MapClientEventToSimEvent({name})")

    def transmit(self, event_id: int, data: int = 0) -> None:
        self._dll.SimConnect_TransmitClientEvent(self._handle, OBJECT_ID_USER, event_id, to_dword(data),
                                                 GROUP_PRIORITY_HIGHEST, EVENT_FLAG_GROUPID_IS_PRIORITY)

    def listen_event(self, group_id: int, event_id: int) -> None:
        # Not maskable: the event still reaches the aircraft as usual, we only observe it.
        self._dll.SimConnect_AddClientEventToNotificationGroup(self._handle, group_id, event_id, 0)
        self._dll.SimConnect_SetNotificationGroupPriority(self._handle, group_id, GROUP_PRIORITY_HIGHEST)

    def subscribe_system_event(self, event_id: int, name: str) -> None:
        self._dll.SimConnect_SubscribeToSystemEvent(self._handle, event_id, name.encode())

    def unsubscribe_system_event(self, event_id: int) -> None:
        self._dll.SimConnect_UnsubscribeFromSystemEvent(self._handle, event_id)

    def request_system_state(self, request_id: int, state: str) -> None:
        self._dll.SimConnect_RequestSystemState(self._handle, request_id, state.encode())

    def wait(self, timeout_s: float) -> None:
        if self._event:
            self._k32.WaitForSingleObject(self._event, max(0, int(timeout_s * 1000)))

    def poll(self) -> Iterable[object]:
        messages: List[object] = []
        if not self._handle:
            return messages
        ptr = c_void_p()
        size = c_uint32()
        while True:
            hr = self._dll.SimConnect_GetNextDispatch(self._handle, byref(ptr), byref(size))
            if hr != S_OK or not ptr.value:
                break
            raw = ctypes.string_at(ptr.value, size.value)
            msg = parse_message(raw)
            if msg is not None:
                messages.append(msg)
        return messages


def _cstr(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("utf-8", "replace")


def parse_message(raw: bytes) -> Optional[object]:
    """Convert one raw SIMCONNECT_RECV block into a FlyRoll message object."""
    if len(raw) < 12:
        return None
    _size, _version, rid = HEADER.unpack_from(raw, 0)
    if rid == RECV_OPEN:
        name = _cstr(raw[12:12 + 256])
        major, minor = struct.unpack_from("<II", raw, 12 + 256) if len(raw) >= 276 else (0, 0)
        return MsgOpen(name, f"{major}.{minor}")
    if rid == RECV_QUIT:
        return MsgQuit()
    if rid == RECV_EXCEPTION:
        code, send_id, index = struct.unpack_from("<III", raw, 12)
        return MsgException(code, send_id, index)
    if rid in (RECV_SIMOBJECT_DATA, RECV_SIMOBJECT_DATA_BYTYPE):
        request_id, _obj, define_id = struct.unpack_from("<III", raw, 12)
        return MsgData(request_id, define_id, raw[DATA_OFFSET:])
    if rid == RECV_EVENT_FRAME:
        _group, event_id = struct.unpack_from("<II", raw, 12)
        fps, speed = struct.unpack_from("<ff", raw, 24)
        return MsgFrame(event_id, fps, speed)
    if rid == RECV_EVENT_FILENAME:
        _group, event_id = struct.unpack_from("<II", raw, 12)
        return MsgFilename(event_id, _cstr(raw[24:24 + MAX_PATH]))
    if rid == RECV_EVENT:
        _group, event_id, data = struct.unpack_from("<III", raw, 12)
        return MsgEvent(event_id, to_int32(data))
    if rid == RECV_SYSTEM_STATE:
        request_id, integer = struct.unpack_from("<II", raw, 12)
        (number,) = struct.unpack_from("<f", raw, 20)
        return MsgSystemState(request_id, to_int32(integer), number, _cstr(raw[24:24 + MAX_PATH]))
    return None


def decode_payload(payload: bytes, fields: Sequence[Field]) -> list:
    """Decode a data block for the given definition (FLOAT64 and STRING256 only)."""
    out = []
    pos = 0
    need = payload_size(fields)
    payload = payload[:need]
    for f in fields:
        if f.kind == F64:
            out.append(struct.unpack_from("<d", payload, pos)[0] if pos + 8 <= len(payload) else 0.0)
            pos += 8
        else:
            out.append(_cstr(payload[pos:pos + 256]))
            pos += 256
    return out
