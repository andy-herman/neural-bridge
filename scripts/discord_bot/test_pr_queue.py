"""`approve <id>` / `cancel <id>` in Discord, now that proposals are review
queue action items. The queue is a temp database and the push is a fake;
the handler and the store are real."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.discord_bot import handlers  # noqa: E402
from scripts.discord_bot import repos as repos_mod  # noqa: E402
from scripts.discord_bot.pr_proposals import PRProposal  # noqa: E402
from scripts.review_queue import actions  # noqa: E402
from scripts.review_queue import store as st  # noqa: E402

CHANNEL = 4242


class FakeChannel:
    def __init__(self):
        self.id = CHANNEL
        self.sent: list[str] = []

    async def send(self, text, **kw):
        self.sent.append(text)


class TestDiscordApproval(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {st.DB_ENV: str(Path(self._tmp.name) / "q.db")})
        self._env.start()
        self.store = st.Store()
        self.pushed: list[str] = []

        def fake_apply(item):
            if item.verb == st.APPROVE:
                self.pushed.append(item.id)
                return True, "https://github.com/andy-herman/neural-bridge-blog/pull/77"
            return True, "dropped; nothing was pushed"

        self._apply = mock.patch.object(handlers.queue_actions, "apply", side_effect=fake_apply)
        self._apply.start()
        self.channel = FakeChannel()

    def tearDown(self):
        self._apply.stop()
        self._env.stop()
        self._tmp.cleanup()

    def stage(self, agent="content", channel=CHANNEL, pid="p1", now=None):
        p = PRProposal(proposal_id=pid, agent_id=agent, channel_id=channel,
                       repo=repos_mod.REPOS["neural-bridge-blog"], branch="content/x",
                       files=[("src/x.mdx", "body")], commit_message="m", pr_title="Publish x",
                       pr_body="b")
        return actions.stage(p, store=self.store, now=now)

    async def say(self, text, agent="content"):
        client = SimpleNamespace(agent=SimpleNamespace(id=agent))
        message = SimpleNamespace(content=text, channel=self.channel, author=SimpleNamespace(id=7))
        return await handlers.handle_pr_approval(client, message, None)

    async def test_approve_by_id_pushes_once_and_records_it(self):
        item = self.stage()
        self.assertTrue(await self.say(f"approve {item.id}"))
        self.assertEqual(self.pushed, [item.id])
        after = self.store.get(item.id)
        self.assertEqual((after.state, after.verb), (st.APPLIED, st.APPROVE))
        self.assertIn("pull/77", after.result)
        self.assertIn("✅ PR opened", self.channel.sent[-1])
        actors = {e["actor"] for e in self.store.events(item.id)}
        self.assertIn("discord:7", actors)

    async def test_plain_approve_takes_the_latest_pending_proposal(self):
        self.stage(pid="p1")
        newest = self.stage(pid="p2")
        self.assertTrue(await self.say("ship it"))
        self.assertEqual(self.pushed, [newest.id])

    async def test_cancel_drops_without_pushing(self):
        item = self.stage()
        self.assertTrue(await self.say(f"cancel {item.id}"))
        self.assertEqual(self.pushed, [])
        self.assertEqual(self.store.get(item.id).verb, st.REJECT)
        self.assertIn("Nothing pushed", self.channel.sent[-1])

    async def test_a_proposal_already_decided_on_telegram_is_not_pushed_again(self):
        item = self.stage()
        self.store.decide(item.id, st.APPROVE, actor="telegram:1")
        self.assertTrue(await self.say(f"approve {item.id}"))
        self.assertEqual(self.pushed, [])
        self.assertIn("already handled", self.channel.sent[-1])

    async def test_a_day_old_proposal_expires_instead_of_pushing(self):
        import time
        item = self.stage(now=int(time.time()) - actions.ACTION_TTL_SECONDS - 5)
        self.assertTrue(await self.say(f"approve {item.id}"))
        self.assertEqual(self.pushed, [])
        self.assertEqual(self.store.get(item.id).state, st.SUPERSEDED)
        self.assertIn("expired", self.channel.sent[-1])

    async def test_other_agents_channels_and_chatter_are_not_claimed(self):
        item = self.stage(agent="luna")
        self.assertFalse(await self.say(f"approve {item.id}", agent="content"))
        other = self.stage(channel=CHANNEL + 1, pid="p9")
        self.assertFalse(await self.say(f"approve {other.id}"))
        self.assertFalse(await self.say("approve"), "nothing pending for this agent here")
        self.assertFalse(await self.say("looks good to me, thanks"))
        self.assertEqual(self.pushed, [])


if __name__ == "__main__":
    unittest.main()
