"""Aircraft profiles.

A profile tells FlyRoll how a particular aircraft wants to be driven during
replay.  Study-level airliners (Fenix, iniBuilds, FlyByWire, PMDG ...) run
their own FADEC / thrust-lever logic: they *write* the standard
``GENERAL ENG THROTTLE LEVER POSITION`` simvar every frame from their own
internal lever state.  Writing that simvar from outside - which is what Sky
Dolly does - is overwritten again within one frame, so the levers never move
and the engines stay at idle in the replay.

FlyRoll therefore supports several *throttle methods*:

``simvar``  write GENERAL ENG THROTTLE LEVER POSITION (default aircraft; Sky Dolly behaviour)
``lvar``    write the aircraft's own thrust-lever LVars (e.g. Fenix A_FC_THROTTLE_*_INPUT)
``axis``    re-send the throttle axis input recorded from your hardware
            (THROTTLEx_AXIS_SET_EX1) so the aircraft's own lever logic, detents and
            calibration reproduce exactly what you did; falls back to THROTTLEx_SET
            derived from the recorded lever position when no axis input was recorded
``auto``    lvar if the profile has lever LVars, else axis if axis input was
            recorded, else simvar

and *spool modes* for the engine core values (N1 / N2 / RPM / prop RPM):

``off``     don't touch them - the engine model follows the levers on its own
``assist``  write the recorded values only when the live value drifts too far
            (e.g. recording flown with A/THR active, so the levers sat in CLB)
``force``   write the recorded values every frame

Profiles are JSON files: the built-in ones live in ``flyroll/profiles`` and
user profiles in ``%APPDATA%/FlyRoll/profiles`` (user files with the same id
override built-in ones).
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

THROTTLE_METHODS = ("auto", "simvar", "lvar", "axis")
SPOOL_MODES = ("off", "assist", "force")


@dataclass
class LVarSpec:
    name: str                 # "L:SOMETHING" (or "L:1:..." / "Z:..." scoped vars)
    replay: str = "continuous"  # continuous | discrete | off (record only)
    note: str = ""

    def __post_init__(self) -> None:
        n = self.name.strip()
        if not n.upper().startswith(("L:", "Z:")):
            n = "L:" + n
        self.name = n
        if self.replay not in ("continuous", "discrete", "off"):
            self.replay = "continuous"


@dataclass
class Profile:
    id: str
    name: str
    match: List[str] = field(default_factory=list)
    engines: int = 0                                  # 0 = use NUMBER OF ENGINES
    throttle_method: str = "auto"
    throttle_lvars: List[str] = field(default_factory=list)   # one per engine, recorded and written back 1:1
    write_throttle_simvar: bool = True
    spool: str = "assist"
    spool_tolerance: float = 1.5                      # % N1 / % RPM drift before "assist" writes
    engine_autostart: bool = True                     # ENGINE_AUTO_START / SHUTDOWN like Sky Dolly
    replay_lights: bool = True
    replay_flaps: bool = True
    replay_gear: bool = True
    replay_spoilers: bool = True
    replay_controls: bool = True
    lvars: List[LVarSpec] = field(default_factory=list)
    notes: str = ""
    builtin: bool = False

    # -- matching ---------------------------------------------------------------
    def score(self, haystack: str) -> int:
        """Return a match score (0 = no match). Patterns use '+' for AND."""
        h = haystack.lower()
        best = 0
        for pattern in self.match:
            words = [w.strip().lower() for w in pattern.split("+") if w.strip()]
            if words and all(w in h for w in words):
                best = max(best, sum(len(w) for w in words))
        return best

    def all_lvars(self) -> List[LVarSpec]:
        """Every LVar to record: throttle lever LVars first, then extra LVars."""
        seen = set()
        out: List[LVarSpec] = []
        for name in self.throttle_lvars:
            spec = LVarSpec(name, "continuous", "throttle lever")
            if spec.name not in seen:
                seen.add(spec.name)
                out.append(spec)
        for spec in self.lvars:
            if spec.name not in seen:
                seen.add(spec.name)
                out.append(spec)
        return out

    def to_json(self) -> str:
        d = asdict(self)
        d.pop("builtin", None)
        return json.dumps(d, indent=2)

    @staticmethod
    def from_dict(d: dict, builtin: bool = False) -> "Profile":
        d = dict(d)
        d["lvars"] = [LVarSpec(**l) if isinstance(l, dict) else LVarSpec(str(l)) for l in d.get("lvars", [])]
        d["throttle_lvars"] = [LVarSpec(n).name for n in d.get("throttle_lvars", [])]
        known = {f for f in Profile.__dataclass_fields__}
        p = Profile(**{k: v for k, v in d.items() if k in known})
        p.builtin = builtin
        if p.throttle_method not in THROTTLE_METHODS:
            p.throttle_method = "auto"
        if p.spool not in SPOOL_MODES:
            p.spool = "assist"
        return p


def builtin_profile_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    p = base / "profiles"
    if not p.is_dir():
        p = base / "flyroll" / "profiles"
    return p


def user_data_dir() -> Path:
    root = os.environ.get("FLYROLL_HOME")
    if root:
        d = Path(root)
    elif sys.platform == "win32" and os.environ.get("APPDATA"):
        d = Path(os.environ["APPDATA"]) / "FlyRoll"
    else:
        d = Path.home() / ".flyroll"
    d.mkdir(parents=True, exist_ok=True)
    return d


def user_profile_dir() -> Path:
    d = user_data_dir() / "profiles"
    d.mkdir(parents=True, exist_ok=True)
    return d


DEFAULT_PROFILE = Profile(
    id="default",
    name="Generic aircraft",
    match=[],
    throttle_method="auto",
    write_throttle_simvar=True,
    spool="assist",
    engine_autostart=True,
    notes="Used when no specific profile matches. Behaves like Sky Dolly for the levers "
          "and additionally replays recorded throttle axis input and engine spool.",
    builtin=True,
)


class ProfileStore:
    def __init__(self, builtin_dir: Optional[Path] = None, user_dir: Optional[Path] = None) -> None:
        self.builtin_dir = builtin_dir or builtin_profile_dir()
        self.user_dir = user_dir or user_profile_dir()
        self.profiles: Dict[str, Profile] = {}
        self.reload()

    def reload(self) -> None:
        self.profiles = {DEFAULT_PROFILE.id: DEFAULT_PROFILE}
        for directory, builtin in ((self.builtin_dir, True), (self.user_dir, False)):
            if not directory or not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.json")):
                try:
                    p = Profile.from_dict(json.loads(path.read_text(encoding="utf-8")), builtin)
                    self.profiles[p.id] = p
                except Exception as exc:  # broken user file must not kill the app
                    print(f"FlyRoll: ignoring profile {path}: {exc}", file=sys.stderr)

    def get(self, profile_id: str) -> Profile:
        return self.profiles.get(profile_id, DEFAULT_PROFILE)

    def detect(self, *texts: str) -> Profile:
        haystack = " | ".join(t for t in texts if t)
        best, best_score = DEFAULT_PROFILE, 0
        for p in self.profiles.values():
            s = p.score(haystack)
            if s > best_score:
                best, best_score = p, s
        return best

    def save_user(self, profile: Profile) -> Path:
        path = self.user_dir / f"{profile.id}.json"
        path.write_text(profile.to_json(), encoding="utf-8")
        self.reload()
        return path

    def sorted(self) -> List[Profile]:
        return sorted(self.profiles.values(), key=lambda p: (p.id != "default", p.name.lower()))
