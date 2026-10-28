# FlyRoll

Flight recorder & replay for **Microsoft Flight Simulator 2024 and 2020** that also records and replays the
**engines** — thrust-lever positions, N1, N2, RPM, prop RPM, EGT, ITT, fuel flow and reversers — for up to four
engines, plus aircraft-specific local variables (LVars). Built for study-level airliners such as the
**Fenix A319/A320/A321** and the **iniBuilds A340 / A350 / A380**, and it works with every other aircraft too.

FlyRoll is a new, independent project. It uses [Sky Dolly](https://github.com/till213/SkyDolly) (MIT) as its
reference; the interpolation and the flaps/lights/gear replay logic are ported from it (see `THIRD_PARTY.md`).

---

## Why Sky Dolly doesn't replay the engines on these aircraft

I went through Sky Dolly's SimConnect plugin. There are three separate problems:

1. **Engine RPM is never recorded.** Sky Dolly's engine data (`SimConnectEngineCommon/Core/Event.h`) contains only
   throttle / propeller / mixture *lever* positions, cowl flaps, battery, starter and a combustion flag.
   There is no N1, N2, RPM, prop RPM, EGT or fuel flow at all — so there is nothing to replay.
2. **The throttle replay is overwritten.** Sky Dolly replays the levers by writing
   `GENERAL ENG THROTTLE LEVER POSITION`. Fenix, iniBuilds, FlyByWire, PMDG and other FADEC aircraft run their own
   thrust-lever logic and *rewrite that simvar every frame* from their internal lever state (the Fenix keeps it
   in `L:A_FC_THROTTLE_LEFT_INPUT` / `RIGHT_INPUT`). The value Sky Dolly writes is gone one frame later, so the
   levers sit at idle and the engines never spool up in the replay. It looks fine in default aircraft, which
   is why the problem only shows up on add-on airliners.
3. **Strobe-light bug.** In `EventStateHandler::sendLight` the strobe light is fed the *taxi* light state
   (`m_strobeLightToggle.requested = event.taxi;`), so strobes follow the taxi lights during replay.

## How FlyRoll fixes it

| | Sky Dolly | FlyRoll |
|---|---|---|
| Engine values recorded | levers only | levers + N1, N2, corrected N1, RPM, prop RPM, EGT, ITT, fuel flow, reverser, combustion, starter — per engine (1–4) |
| Thrust levers on FADEC aircraft | writes the simvar → overwritten | drives the aircraft's **own** inputs: its thrust-lever **LVars** (Fenix) or your **recorded throttle-axis input** re-sent through the aircraft's own calibration (iniBuilds, FBW, PMDG) |
| Autothrust flights | engines idle | **N1/N2 assist** writes the recorded N1/N2 (or RPM) whenever the live engine drifts from the recording |
| Aircraft specifics | — | JSON **aircraft profiles**, auto-detected from the loaded aircraft, plus an **LVar tester** to add any LVar |
| Strobe lights | follow taxi lights | own state |

Throttle methods (Replay settings → *Thrust levers*):

- **Aircraft thrust-lever LVars** – writes e.g. `L:A_FC_THROTTLE_LEFT_INPUT` (Fenix: 0 full reverse, 1 idle reverse,
  2 idle, 3 CLB, 4 FLX/MCT, 5 TOGA).
- **Recorded hardware throttle axis** – while recording FlyRoll listens to the `THROTTLEx_AXIS_SET_EX1`,
  `AXIS_THROTTLEx_SET` and `THROTTLEx_SET` events your throttle quadrant sends; on replay it sends them again, so the
  aircraft's detents and calibration produce exactly the lever positions you flew. If no axis input was recorded
  (keyboard, autothrust) it falls back to `THROTTLEx_SET` from the recorded lever position.
- **Throttle simvar** – Sky Dolly's method, fine for default aircraft.
- **Auto** – LVars if the profile has them, else axis, else simvar.

N1/N2/RPM (*spool*): **Assist** (default) corrects drift beyond 1.5 %, **Force** writes every frame, **Off** lets the
engine model follow the levers.

## Built-in aircraft profiles

| Profile | Detected by | Thrust levers | Notes |
|---|---|---|---|
| Fenix A319/A320/A321 | `fenix`, `fnx-aircraft` … | LVars `A_FC_THROTTLE_LEFT/RIGHT_INPUT` | auto engine start off |
| iniBuilds A350, A340, A380, A300/A310, A330 | `inibuilds` + type | hardware axis | N1 assist, 2 or 4 engines |
| FSLabs A321 | `fslabs` | hardware axis | N1 assist, stick input not replayed (fly-by-wire) |
| iFly 737 MAX 8 | `ifly` | hardware axis + simvar | conventional controls replayed |
| FlyByWire A32NX / A380X | `a32nx`, `a380x` | hardware axis | also records `A32NX_AUTOTHRUST_TLA:n` |
| PMDG 737 / 777 | `pmdg` | hardware axis + simvar | |
| Generic | everything else | auto | Sky Dolly behaviour + axis + N1 assist |

Profiles are plain JSON (`flyroll/profiles/*.json`). Your own profiles are stored in `%APPDATA%\FlyRoll\profiles`
and override the built-in ones with the same `id`. I could not verify iniBuilds' internal thrust-lever LVar
names, so those profiles use the axis method; if you find the names, add them in the **Profiles & LVars** tab:

1. In MSFS enable Developer Mode → *Tools → Behaviors* → *LocalVariables* tab, filter by `INI` (or the aircraft prefix).
2. Type the name in FlyRoll's **LVar tester** and press *Watch* — move the thrust lever and see whether the value follows.
3. Select it and click **Add selected as thrust-lever LVars** (one per engine, in engine order) or
   **Add selected as recorded LVars** for switches, lights, doors …

## Install & run (Windows)

**Option A – run from source** (needs Python 3.10+ from python.org):

```bat
run.bat
```

The first start creates a virtual environment and installs PySide6, numpy and the `SimConnect` package (only for its
`SimConnect.dll`).

**Option B – build a standalone exe:**

```bat
build.bat
```

runs the tests and produces `dist\FlyRoll\FlyRoll.exe`. For best MSFS 2024 compatibility copy
`C:\MSFS 2024 SDK\SimConnect SDK\lib\SimConnect.dll` next to `FlyRoll.exe` (or next to `FlyRoll.spec` before
building). FlyRoll looks for the DLL next to the exe, in the MSFS 2024/2020 SDK folders, in the `SimConnect` pip package,
or at the path in `FLYROLL_SIMCONNECT_DLL` / `--dll`.

Pushing the folder to GitHub also builds the exe automatically (`.github/workflows/build.yml`).

**Try it without the simulator:** `run.bat --demo fenix` (or `a350`, `a380`, `c172`) starts a built-in mock aircraft
that behaves like a FADEC airliner, so you can record, replay and watch the engines spool.

## Autothrust and N1 follow

With autothrust the levers sit in the CLB detent while the FADEC varies N1. Copying that lever into a
replay gives full climb thrust, so for the Fenix FlyRoll copies the lever only at idle, reverse, FLX/MCT
and TOGA. Between IDLE and CLB it steers the lever so the live N1 follows the recorded N1 (feed-forward
from the recorded N1 trend plus a slow correction that learns the aircraft's lever-to-N1 mapping).
Switch it off under *Replay settings → Thrust follows recorded N1*.

## Updates

FlyRoll checks GitHub for a newer release a few seconds after it starts. When one exists a green
**⬆ Update to vX.Y.Z** button appears in the header: it shows the release notes, downloads the zip
(size and SHA-256 checked), closes FlyRoll, copies the new files over the install folder and starts it
again. The logbook and your profiles live in `%APPDATA%\FlyRoll` and are never touched.
The **⋯** menu has *Check for updates now* and an option to turn the startup check off; *Skip this version*
hides a release you don't want.

If FlyRoll sits in a folder it can't write to (e.g. `C:\Program Files`) or you run it from source,
the button opens the download page instead.

**Publishing a new version** (maintainer): push a tag — the workflow stamps the tag into the app version,
builds the exe and publishes the release that every installed copy picks up:

```bat
git tag v1.2.0
git push origin v1.2.0
```

## Using it

1. Start MSFS and load your flight; FlyRoll connects automatically (green **Connected** pill) and shows the detected profile.
2. **● REC** (Ctrl+R) to record, again to stop — the flight is saved to the logbook.
3. Drag the timeline at any time — the aircraft follows the slider live (a paused replay starts automatically
   if none is running). Click anywhere on the timeline to jump there.
4. Select a flight and **▶ Play** (Space). Your aircraft is frozen and moved along the recording; levers, N1/N2,
   lights, flaps, gear, spoilers and LVars follow. Drag the timeline to seek, change speed, loop. **■ Stop** unfreezes it.
5. The **Engines** tab shows live N1 (or RPM), lever, N2, EGT and fuel flow per engine; cyan marks show the recorded values.

Tips for study-level aircraft:

- Start the replay in the same aircraft with the **engines already running** (Fenix/iniBuilds ignore `ENGINE_AUTO_START`).
- For the axis method, bind your throttle to the standard *Throttle axis* / *Throttle 1–4 axis* controls and don't
  touch it during the replay.
- Timestamps follow the simulator clock, so pausing MSFS while recording doesn't leave gaps.

Logbook: `%APPDATA%\FlyRoll\logbook.sqlite` (export/import single flights as `.flyroll`, or CSV).

## Changes in 1.1

- **Ailerons / roll spoilers deflecting on fly-by-wire aircraft:** the stick input was being replayed into the
  Fenix's flight-control computers, which treat it as a roll-rate demand; with the replay holding the aircraft
  they kept increasing the deflection. Airbus profiles (Fenix, FSLabs, iniBuilds A330/A340/A350/A380, FBW) no
  longer inject stick input, and the axes are re-centred at replay start/stop for every aircraft.
- **Timeline scrubbing:** the aircraft follows the slider while you drag it; click-to-jump on the timeline.
- **Spoilers reset when scrubbing back into the air:** FlyRoll now records the spoiler *surfaces*; when the
  recording shows them stowed but the aircraft's auto-deployed ground spoilers are out, it disarms, retracts and
  re-arms after 1 s if the recording was armed.
- **Wing flex with reversers:** wing flex is now recorded and written back during replay (if the sim refuses
  the write, FlyRoll switches to the flight-model flex automatically and says so in the log), body velocities
  are smoothed over 0.6 s, and rotation rates are no longer written by default. All three are switchable under
  *Replay settings*.
- **New profiles:** FSLabs A321 and iFly 737 MAX 8.

## Project layout

```
FlyRoll.py               launcher (PyInstaller entry)
flyroll/sim/native.py    SimConnect via ctypes (no compiled code)
flyroll/sim/mock.py      demo/test simulator with Fenix- and iniBuilds-like FADEC behaviour
flyroll/core/channels.py everything that is recorded (~115 channels + LVars + throttle axes)
flyroll/core/session.py  connection, recording, replay (throttle methods, spool assist, lights/flaps/gear)
flyroll/core/flight.py   flight data, chunked record buffer, Hermite interpolation (from Sky Dolly)
flyroll/core/logbook.py  SQLite logbook, .flyroll / CSV export
flyroll/core/profiles.py aircraft profiles; flyroll/profiles/*.json
flyroll/ui/              PySide6 interface
tests/                   end-to-end tests against the mock simulator
```

Not included compared to Sky Dolly: formation flights / AI aircraft replay, location manager and import/export
plugins (IGC, GPX, KML …).
