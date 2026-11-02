"""Action items: a staged PR proposal in the queue, applied or expired. The
executor is a fake; nothing touches git."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.discord_bot import repos as repos_mod  # noqa: E402
from scripts.discord_bot.pr_proposals import PRProposal  # noqa: E402
from scripts.review_queue import actions  # noqa: E402
from scripts.review_queue import store as st  # noqa: E402

T0 = 1_800_000_000


def proposal(pid="p1", agent="content", channel=42, files=None) -> PRProposal:
    return PRProposal(proposal_id=pid, agent_id=agent, channel_id=channel,
                      repo=repos_mod.REPOS["neural-bridge-blog"], branch="content/new-post",
                      files=files or [("src/content/research/x.mdx", "---\ntitle: X\n---\nbody")],
                      commit_message="feat(research): publish x", pr_title="Publish x",
                      pr_body="Draft from Sunday.")


class ActionCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = st.Store(Path(self._tmp.name) / "q.db")

    def tearDown(self):
        self._tmp.cleanup()


class TestStage(ActionCase):
    def test_a_proposal_becomes_one_durable_action_item(self):
        item = actions.stage(proposal(), store=self.store, now=T0)
        self.assertEqual((item.kind, item.source, item.agent, item.key),
                         (st.ACTION, actions.SOURCE, "content", "p1"))
        self.assertIn("neural-bridge-blog", item.title)
        self.assertIn("Publish x", item.detail)
        self.assertIn("1 file ", item.detail)
        self.assertEqual(actions.stage(proposal(), store=self.store).id, item.id,
                         "staging the same proposal again is a no-op")

    def test_the_proposal_round_trips_through_the_payload(self):
        original = proposal(files=[("a.md", "one"), ("b/c.md", "two")])
        item = actions.stage(original, store=self.store, now=T0)
        back = actions.proposal_from(self.store.get(item.id))
        self.assertEqual(back.proposal_id, item.id)
        for field in ("agent_id", "channel_id", "repo", "branch", "files",
                      "commit_message", "pr_title", "pr_body"):
            self.assertEqual(getattr(back, field), getattr(original, field), field)

    def test_an_unknown_repo_is_refused(self):
        item = actions.stage(proposal(), store=self.store)
        item.payload["repo"] = "not-a-repo"
        with self.assertRaises(ValueError):
            actions.proposal_from(item)


class TestApply(ActionCase):
    def decided(self, verb):
        item = actions.stage(proposal(), store=self.store, now=T0)
        self.store.decide(item.id, verb, actor="test")
        return self.store.get(item.id)

    def test_approve_executes_the_proposal(self):
        seen = []

        def execute(p):
            seen.append(p)
            return SimpleNamespace(ok=True, pr_url="https://github.com/x/y/pull/7", branch=p.branch, error=None)

        self.assertEqual(actions.apply(self.decided(st.APPROVE), execute=execute),
                         (True, "https://github.com/x/y/pull/7"))
        self.assertEqual(seen[0].branch, "content/new-post")

    def test_a_failed_push_says_whether_a_branch_went_out(self):
        def execute(p):
            return SimpleNamespace(ok=False, pr_url=None, branch="content/new-post", error="gh pr create failed")

        ok, result = actions.apply(self.decided(st.APPROVE), execute=execute)
        self.assertFalse(ok)
        self.assertIn("gh pr create failed", result)
        self.assertIn("may have been pushed", result)

    def test_reject_pushes_nothing(self):
        def execute(p):
            raise AssertionError("a rejected proposal must not execute")

        self.assertEqual(actions.apply(self.decided(st.REJECT), execute=execute)[0], True)


class TestExpiry(ActionCase):
    def test_undecided_actions_expire_after_a_day_and_others_are_left_alone(self):
        old = actions.stage(proposal("old"), store=self.store, now=T0)
        fresh = actions.stage(proposal("fresh"), store=self.store, now=T0 + 20 * 3600)
        decided = actions.stage(proposal("decided"), store=self.store, now=T0)
        self.store.decide(decided.id, st.APPROVE, actor="test", now=T0 + 3600)

        expired = actions.expire_stale(self.store, T0 + actions.ACTION_TTL_SECONDS)
        self.assertEqual(expired, [old.id])
        self.assertEqual(self.store.get(old.id).state, st.SUPERSEDED)
        self.assertEqual([e["event"] for e in self.store.events(old.id)][-1], "expired")
        self.assertEqual(self.store.get(fresh.id).state, st.OPEN)
        self.assertEqual(self.store.get(decided.id).state, st.DECIDED)
        self.assertFalse(self.store.decide(old.id, st.APPROVE, actor="test"),
                         "an expired proposal can no longer be approved")


class TestLatestOpen(ActionCase):
    def test_scoped_to_channel_and_agent(self):
        actions.stage(proposal("a", agent="content", channel=1), store=self.store, now=T0)
        b = actions.stage(proposal("b", agent="content", channel=1), store=self.store, now=T0 + 1)
        actions.stage(proposal("c", agent="luna", channel=1), store=self.store, now=T0 + 2)
        actions.stage(proposal("d", agent="content", channel=2), store=self.store, now=T0 + 3)
        self.assertEqual(actions.latest_open(self.store, channel_id=1, agent_id="content").id, b.id)
        self.assertIsNone(actions.latest_open(self.store, channel_id=3, agent_id="content"))


if __name__ == "__main__":
    unittest.main()
