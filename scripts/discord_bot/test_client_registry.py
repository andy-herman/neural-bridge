"""Unit tests for client_registry.py."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG_DIR.parent.parent))

from scripts.discord_bot.client_registry import REGISTRY, ClientRegistry  # noqa: E402


class TestClientRegistry(unittest.TestCase):
    def setUp(self):
        self.reg = ClientRegistry()

    def test_empty_starts_clean(self):
        self.assertEqual(len(self.reg), 0)
        self.assertIsNone(self.reg.get("research"))
        self.assertEqual(self.reg.all_ids(), [])

    def test_register_and_lookup(self):
        sentinel = object()
        self.reg.register("research", sentinel)
        self.assertIs(self.reg.get("research"), sentinel)
        self.assertEqual(self.reg.all_ids(), ["research"])
        self.assertEqual(len(self.reg), 1)

    def test_register_replaces(self):
        a = object()
        b = object()
        self.reg.register("research", a)
        self.reg.register("research", b)
        self.assertIs(self.reg.get("research"), b)
        self.assertEqual(len(self.reg), 1)

    def test_all_ids_sorted(self):
        for name in ("social", "research", "content"):
            self.reg.register(name, object())
        self.assertEqual(self.reg.all_ids(), ["content", "research", "social"])

    def test_reset(self):
        self.reg.register("x", object())
        self.reg.reset()
        self.assertEqual(len(self.reg), 0)


class TestModuleSingleton(unittest.TestCase):
    def setUp(self):
        REGISTRY.reset()

    def tearDown(self):
        REGISTRY.reset()

    def test_singleton_is_instance(self):
        self.assertIsInstance(REGISTRY, ClientRegistry)

    def test_singleton_persists_across_imports(self):
        REGISTRY.register("x", object())
        # Re-import the same singleton
        from scripts.discord_bot.client_registry import REGISTRY as also_registry
        self.assertEqual(len(also_registry), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
