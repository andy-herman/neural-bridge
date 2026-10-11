"""The single plugin-shipped companion contract, also loaded by NB wrappers."""

from __future__ import annotations

from pathlib import Path

COMPANION_SKILL_ID = "neural-bridge-core:companion-standard"
COMPANION_STANDARD_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugins" / "neural-bridge-core" / "skills" / "companion-standard" / "SKILL.md"
)


class CompanionSetupError(Exception):
    """A required contract could not be loaded; safe to surface without contents."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def load_companion_standard(path: Path | None = None) -> str:
    """Load full skill content without metadata; never degrade to a generic role."""
    path = path if path is not None else COMPANION_STANDARD_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise CompanionSetupError(
            "companion_standard_missing",
            "Companion standard missing: restore companion-standard/SKILL.md. No model was called.",
        ) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise CompanionSetupError(
            "companion_standard_unreadable",
            f"Companion standard unreadable ({type(exc).__name__}). No model was called.",
        ) from None

    if text.startswith("---"):
        end = text.find("\n---\n", 4)
        if end == -1:
            raise CompanionSetupError(
                "companion_standard_invalid",
                "Companion standard has incomplete metadata. No model was called.",
            )
        text = text[end + 5:]
    body = text.strip()
    if not body:
        raise CompanionSetupError(
            "companion_standard_empty",
            "Companion standard is empty. No model was called.",
        )
    return body
