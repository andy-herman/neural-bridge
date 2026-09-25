"""
Neural Bridge → Fleet dashboard heartbeat.

Thin wrapper around ~/Documents/Luna Master/Fleet/scripts/heartbeat.py so
the Discord bot daemon and the compile pipeline can emit without
repeating slug/name/runtime boilerplate. If the Fleet dir is missing
(e.g. running on a different machine, CI), every call silently no-ops.

Calls also no-op under a test runner. The heartbeat file is the live fleet
dashboard's source of truth, and on the Mac the Fleet dir exists, so before
this guard every local test run stamped fake "compile (live): PROMOTE=3"
events and a fresh last_activity into it (found 2026-09-25). Same rule as
memory_telemetry: output that lies about the system is worse than none.
"""
import os
import sys
from pathlib import Path

_SLUG = "neural-bridge"
_NAME = "Neural Bridge"
_RUNTIME = "launchd + caffeinate, Mac Mini M4"

_FLEET_SCRIPTS = Path.home() / "Documents" / "Luna Master" / "Fleet" / "scripts"

if _FLEET_SCRIPTS.exists() and str(_FLEET_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_FLEET_SCRIPTS))

try:
    from heartbeat import emit as _emit, touch as _touch
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False

ENV_DISABLE = "NB_NO_FLEET_HEARTBEAT"


def _suppressed() -> bool:
    if os.environ.get(ENV_DISABLE) == "1":
        return True
    return "unittest" in sys.modules or "pytest" in sys.modules


def set_state(metrics=None, headline=None, self_status="running"):
    if not _AVAILABLE or _suppressed():
        return
    _emit(
        slug=_SLUG,
        name=_NAME,
        runtime=_RUNTIME,
        self_status=self_status,
        metrics=metrics,
        headline=headline,
    )


def log_event(label: str):
    if not _AVAILABLE or _suppressed():
        return
    try:
        _touch(_SLUG, event_label=label)
    except FileNotFoundError:
        set_state()
        _touch(_SLUG, event_label=label)
