"""FlyRoll - flight recorder & replay for Microsoft Flight Simulator 2020 / 2024.

Records the full aircraft state - position, attitude, controls, lights, flaps,
gear AND engine state (throttle levers, N1/N2, RPM, prop RPM, EGT, fuel flow,
reversers) plus aircraft specific local variables (LVars) used by study-level
aircraft such as the Fenix A320 family and the iniBuilds A340/A350/A380.

Inspired by and partially derived from Sky Dolly (MIT licensed, (c) Oliver Knoll),
see THIRD_PARTY.md.
"""

__version__ = "1.1.0"
APP_NAME = "FlyRoll"
