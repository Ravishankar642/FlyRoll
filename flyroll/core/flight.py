"""Flight data model, recording buffer and replay interpolation."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np

from .channels import (ANGLE180, ANGLE360, HERMITE, HERMITE180, LINEAR, STEP, Channel)

FORMAT_VERSION = 1


@dataclass
class Flight:
    channels: List[Channel]
    t: np.ndarray                      # seconds since start, float64, strictly increasing
    columns: Dict[str, np.ndarray]     # key -> values (len == len(t))
    meta: dict = field(default_factory=dict)
    id: Optional[int] = None

    @property
    def duration(self) -> float:
        return float(self.t[-1]) if len(self.t) else 0.0

    @property
    def samples(self) -> int:
        return int(len(self.t))

    def channel(self, key: str) -> Optional[Channel]:
        for c in self.channels:
            if c.key == key:
                return c
        return None

    def has(self, key: str) -> bool:
        col = self.columns.get(key)
        return col is not None and bool(np.isfinite(col).any())

    def sampler(self) -> "Sampler":
        return Sampler(self)

    def summary(self) -> dict:
        return {
            "title": self.meta.get("title", ""),
            "profile": self.meta.get("profile_id", "default"),
            "duration": self.duration,
            "samples": self.samples,
        }


class RecordBuffer:
    """Append-only, chunked storage used while recording.

    Values arrive as one float64 vector per sample (the order of
    ``channels``).  Precise channels are kept as float64, the rest as float32
    to halve memory (2 h at 30 Hz with ~110 channels is roughly 100 MB).
    """

    CHUNK = 4096

    def __init__(self, channels: Sequence[Channel]) -> None:
        self.channels = list(channels)
        self.precise_idx = np.array([i for i, c in enumerate(self.channels) if c.precise], dtype=np.intp)
        self.normal_idx = np.array([i for i, c in enumerate(self.channels) if not c.precise], dtype=np.intp)
        self._t_chunks: List[np.ndarray] = []
        self._p_chunks: List[np.ndarray] = []
        self._n_chunks: List[np.ndarray] = []
        self._fill = self.CHUNK  # force new chunk on first append
        self.count = 0

    def _new_chunk(self) -> None:
        self._t_chunks.append(np.empty(self.CHUNK, dtype=np.float64))
        self._p_chunks.append(np.empty((self.CHUNK, len(self.precise_idx)), dtype=np.float64))
        self._n_chunks.append(np.empty((self.CHUNK, len(self.normal_idx)), dtype=np.float32))
        self._fill = 0

    def append(self, t: float, values: np.ndarray) -> None:
        if self._fill >= self.CHUNK:
            self._new_chunk()
        i = self._fill
        self._t_chunks[-1][i] = t
        self._p_chunks[-1][i] = values[self.precise_idx]
        self._n_chunks[-1][i] = values[self.normal_idx]
        self._fill += 1
        self.count += 1

    def last_time(self) -> float:
        if not self.count:
            return -math.inf
        return float(self._t_chunks[-1][self._fill - 1])

    def to_flight(self, meta: dict) -> Flight:
        if not self.count:
            t = np.zeros(0)
            p = np.zeros((0, len(self.precise_idx)))
            n = np.zeros((0, len(self.normal_idx)), dtype=np.float32)
        else:
            def cat(chunks):
                parts = chunks[:-1] + [chunks[-1][: self._fill]]
                return np.concatenate(parts, axis=0)
            t, p, n = cat(self._t_chunks), cat(self._p_chunks), cat(self._n_chunks)
        columns: Dict[str, np.ndarray] = {}
        for j, i in enumerate(self.precise_idx):
            columns[self.channels[i].key] = p[:, j].copy()
        for j, i in enumerate(self.normal_idx):
            columns[self.channels[i].key] = n[:, j].copy()
        meta = dict(meta)
        meta.setdefault("format", FORMAT_VERSION)
        meta.setdefault("created", time.strftime("%Y-%m-%d %H:%M:%S"))
        return Flight(self.channels, t.copy(), columns, meta)


# ---------------------------------------------------------------------------
# Interpolation (ported from Sky Dolly's SkyMath)
# ---------------------------------------------------------------------------
def hermite(y0, y1, y2, y3, mu, tension=0.0, bias=0.0):
    mu2 = mu * mu
    mu3 = mu2 * mu
    m0 = (y1 - y0) * (1 + bias) * (1 - tension) / 2 + (y2 - y1) * (1 - bias) * (1 - tension) / 2
    m1 = (y2 - y1) * (1 + bias) * (1 - tension) / 2 + (y3 - y2) * (1 - bias) * (1 - tension) / 2
    a0 = 2 * mu3 - 3 * mu2 + 1
    a1 = mu3 - 2 * mu2 + mu
    a2 = mu3 - mu2
    a3 = -2 * mu3 + 3 * mu2
    return a0 * y1 + a1 * m0 + a2 * m1 + a3 * y2


def _unwrap_to(ref, y, period):
    half = period / 2
    return ref + ((y - ref + half) % period - half)


def wrap180(x):
    return (x + 180.0) % 360.0 - 180.0


def lerp_angle(a, b, mu, period=360.0):
    return a + (_unwrap_to(a, b, period) - a) * mu


class Sampler:
    """Evaluates a flight at an arbitrary time ``t`` (seconds)."""

    def __init__(self, flight: Flight, overrides: Optional[Dict[str, np.ndarray]] = None) -> None:
        self.flight = flight
        self.t = flight.t
        self.keys = [c.key for c in flight.channels if c.key in flight.columns]
        self.kind = {c.key: c.interp for c in flight.channels}
        # Step channels may contain NaN (e.g. throttle axis before first input): forward fill
        self.cols: Dict[str, np.ndarray] = {}
        for k in self.keys:
            src = overrides[k] if overrides and k in overrides else flight.columns[k]
            col = np.asarray(src, dtype=np.float64)
            if self.kind.get(k) == STEP and np.isnan(col).any():
                col = _ffill(col)
            self.cols[k] = col

    def index(self, t: float):
        """Return (i1, i2, mu) such that t lies between samples i1 and i2."""
        n = len(self.t)
        if n == 0:
            return 0, 0, 0.0
        if t <= self.t[0]:
            return 0, 0, 0.0
        if t >= self.t[-1]:
            return n - 1, n - 1, 0.0
        i2 = int(np.searchsorted(self.t, t, side="right"))
        i1 = i2 - 1
        dt = self.t[i2] - self.t[i1]
        mu = float((t - self.t[i1]) / dt) if dt > 0 else 0.0
        return i1, i2, mu

    def value(self, key: str, t: float, idx=None) -> float:
        col = self.cols.get(key)
        if col is None or not len(col):
            return math.nan
        i1, i2, mu = idx if idx is not None else self.index(t)
        kind = self.kind.get(key, LINEAR)
        y1, y2 = col[i1], col[i2]
        if kind == STEP or i1 == i2:
            return float(y1)
        if kind in (HERMITE, HERMITE180):
            n = len(col)
            y0 = col[max(i1 - 1, 0)]
            y3 = col[min(i2 + 1, n - 1)]
            if kind == HERMITE180:
                y0n = y0
                y1n = _unwrap_to(y0n, y1, 360.0)
                y2n = _unwrap_to(y1n, y2, 360.0)
                y3n = _unwrap_to(y2n, y3, 360.0)
                return float(wrap180(hermite(y0n, y1n, y2n, y3n, mu)))
            return float(hermite(y0, y1, y2, y3, mu))
        if kind == ANGLE360:
            return float(lerp_angle(y1, y2, mu) % 360.0)
        if kind == ANGLE180:
            return float(wrap180(lerp_angle(y1, y2, mu)))
        return float(y1 + (y2 - y1) * mu)

    def frame(self, t: float, keys: Optional[Sequence[str]] = None) -> Dict[str, float]:
        idx = self.index(t)
        return {k: self.value(k, t, idx) for k in (keys or self.keys)}


def _ffill(col: np.ndarray) -> np.ndarray:
    col = col.copy()
    mask = np.isnan(col)
    idx = np.where(~mask, np.arange(len(col)), 0)
    np.maximum.accumulate(idx, out=idx)
    out = col[idx]
    # leading NaNs stay NaN (no input recorded yet)
    first = np.argmax(~mask) if (~mask).any() else len(col)
    out[:first] = np.nan
    return out


def smoothed(flight: Flight, keys: Sequence[str], seconds: float) -> Dict[str, np.ndarray]:
    """Centered moving average over ``seconds`` (time-based window, edges handled).

    Used for the body velocities written during replay: raw recorded
    velocities on a bumpy rollout contain frame-to-frame jitter which the
    flight model turns into acceleration spikes (wing flex, sounds, shaking).
    """
    out: Dict[str, np.ndarray] = {}
    t = flight.t
    if len(t) < 3:
        return out
    dt = float(np.median(np.diff(t))) or 1 / 30
    n = max(1, int(round(seconds / dt)))
    if n < 2:
        return out
    kernel = np.ones(n) / n
    for k in keys:
        col = flight.columns.get(k)
        if col is None:
            continue
        x = np.asarray(col, dtype=np.float64)
        if not np.isfinite(x).all():
            continue
        pad = n // 2
        xp = np.concatenate([np.full(pad, x[0]), x, np.full(n - 1 - pad, x[-1])])
        out[k] = np.convolve(xp, kernel, mode="valid")[: len(x)]
    return out
