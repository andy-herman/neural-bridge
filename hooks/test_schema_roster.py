"""schema.KNOWN_AGENTS must match the plugin's agents/*.md files exactly.

The agent list was once copied into three hook files and they disagreed
(9, 13, and 13 entries against 14 plugin definitions), so luna, echo,
librarian, and ux-designer got no SessionStart context and loid sessions were
filed as unattributed. schema.py is now the single source; this test pins it
to the plugin directory so the next roster change fails loudly.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
REPO_ROOT = HOOKS_DIR.parent
PLUGIN_AGENTS_DIR = REPO_ROOT / "plugins" / "neural-bridge-core" / "agents"
sys.path.insert(0, str(HOOKS_DIR))

import schema  # noqa: E402
import session_end  # noqa: E402
import session_start  # noqa: E402


class TestSchemaRoster(unittest.TestCase):
    def test_plugin_agents_match_on_disk_definitions(self):
        on_disk = {p.stem for p in PLUGIN_AGENTS_DIR.glob("*.md") if not p.name.startswith(".")}
        self.assertEqual(
            set(schema.PLUGIN_AGENTS), on_disk,
            "schema.PLUGIN_AGENTS and plugins/neural-bridge-core/agents/*.md disagree; "
            "update hooks/schema.py when adding or removing an agent",
        )

    def test_known_agents_is_plugin_agents_plus_unattributed(self):
        self.assertEqual(schema.KNOWN_AGENTS, set(schema.PLUGIN_AGENTS) | {schema.UNATTRIBUTED})

    def test_compile_is_not_a_known_agent(self):
        # compile.py sets NB_AGENT=compile; those sessions must stay unattributed
        # so the compiler never ingests summaries of its own gate calls.
        self.assertNotIn("compile", schema.KNOWN_AGENTS)

    def test_session_hooks_share_the_schema_list(self):
        self.assertIs(session_start.KNOWN_AGENTS, schema.KNOWN_AGENTS)
        self.assertIs(session_end.KNOWN_AGENTS, schema.KNOWN_AGENTS)
        self.assertEqual(session_start.UNATTRIBUTED, schema.UNATTRIBUTED)
        self.assertEqual(session_end.UNATTRIBUTED, schema.UNATTRIBUTED)


if __name__ == "__main__":
    unittest.main()
