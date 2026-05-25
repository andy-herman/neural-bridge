"""
Neural Bridge → Fleet dashboard heartbeat.

Thin wrapper around ~/Documents/Luna Master/Fleet/scripts/heartbeat.py so
the Discord bot daemon and the compile pipeline can emit without
repeating slug/name/runtime boilerplate. If the Fleet dir is missing
(e.g. running on a different machine, CI), every call silently no-ops.
"""
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


def set_state(metrics=None, headline=None, self_status="running"):
    if not _AVAILABLE:
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
    if not _AVAILABLE:
        return
    try:
        _touch(_SLUG, event_label=label)
    except FileNotFoundError:
        set_state()
        _touch(_SLUG, event_label=label)
