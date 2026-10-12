"""PR review items: the loop engineer's PRs synced from GitHub, merged only at
the commit Andy confirmed and only while green. `gh` is a fake that records
every call; nothing reaches GitHub."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.review_queue import pr_reviews as prs  # noqa: E402
from scripts.review_queue import store as st  # noqa: E402

SHA = "a1b2c3d" + "0" * 33
NEW_SHA = "f9e8d7c" + "1" * 33
GREEN_RUN = [{"__typename": "CheckRun", "name": "pytest", "status": "COMPLETED", "conclusion": "SUCCESS"}]


def pr(number=212, branch="eng/88", sha=SHA, checks=GREEN_RUN, draft=True, mergeable="MERGEABLE",
       state="OPEN", title="fix: retry the flush on a locked queue (#88)"):
    return {"number": number, "title": title, "url": f"https://github.com/andy-herman/neural-bridge/pull/{number}",
            "isDraft": draft, "mergeable": mergeable, "headRefOid": sha, "headRefName": branch,
            "statusCheckRollup": checks, "body": "Autonomous implementation of #88.\n\nCloses #88.",
            "state": state}


class FakeGh:
    def __init__(self, prs: list[dict] | None = None, fail: set[str] = frozenset()):
        self.prs = {p["number"]: p for p in (prs or [])}
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, args: list[str]) -> tuple[bool, str]:
        self.calls.append(args)
        verb = " ".join(args[:2])
        if verb in self.fail:
            return False, f"{verb} refused"
        if verb == "pr list":
            return True, json.dumps(list(self.prs.values()))
        if verb == "pr view":
            return True, json.dumps(self.prs[int(args[2])])
        return True, ""

    def verbs(self) -> list[str]:
        return [" ".join(c[:2]) for c in self.calls]


class TestCheckState(unittest.TestCase):
    def test_every_shape_of_rollup(self):
        self.assertEqual(prs.check_state(GREEN_RUN), prs.GREEN)
        self.assertEqual(prs.check_state([]), prs.NO_CHECKS)
        self.assertEqual(prs.check_state([{"status": "IN_PROGRESS", "conclusion": None}]), prs.PENDING)
        self.assertEqual(prs.check_state(GREEN_RUN + [{"status": "COMPLETED", "conclusion": "FAILURE"}]),
                         prs.FAILING)
        self.assertEqual(prs.check_state([{"__typename": "StatusContext", "state": "PENDING"}]), prs.PENDING)
        self.assertEqual(prs.check_state([{"__typename": "StatusContext", "state": "SUCCESS"},
                                          {"status": "COMPLETED", "conclusion": "SKIPPED"}]), prs.GREEN)

    def test_the_issue_comes_from_the_branch_or_the_body(self):
        self.assertEqual(prs.issue_number(pr(branch="eng/88")), 88)
        self.assertEqual(prs.issue_number({"headRefName": "x", "body": "... Closes #41."}), 41)
        self.assertIsNone(prs.issue_number({"headRefName": "x", "body": ""}))


class StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = st.Store(Path(self._tmp.name) / "q.db")
        self.slug = lambda repo_id: "andy-herman/neural-bridge"
        self._orig_slug = prs.repo_slug
        prs.repo_slug = self.slug

    def tearDown(self):
        prs.repo_slug = self._orig_slug
        self._tmp.cleanup()


class TestSync(StoreCase):
    def test_only_loop_branches_become_items_and_closed_ones_clear(self):
        gh = FakeGh([pr(212, "eng/88"), pr(213, "wiki/something-human")])
        created, _ = prs.sync(self.store, gh=gh)
        self.assertEqual([i.title for i in created], ["PR #212: fix: retry the flush on a locked queue (#88)"])
        item = created[0]
        self.assertEqual((item.kind, item.source, item.key), (st.PR_REVIEW, prs.SOURCE, "neural-bridge#212"))
        self.assertIn("Issue #88", item.detail)
        self.assertIn("head a1b2c3d", item.detail)
        self.assertTrue(prs.mergeable_now(item.payload))

        gh.prs.pop(212)
        _, cleared = prs.sync(self.store, gh=gh)
        self.assertEqual([c.id for c in cleared], [item.id])
        self.assertEqual(self.store.get(item.id).state, st.SUPERSEDED)

    def test_a_check_change_refreshes_the_card(self):
        gh = FakeGh([pr(checks=[{"status": "IN_PROGRESS", "conclusion": None}])])
        [item] = prs.sync(self.store, gh=gh)[0]
        self.assertFalse(prs.mergeable_now(item.payload))
        gh.prs[212] = pr()
        prs.sync(self.store, gh=gh)
        after = self.store.get(item.id)
        self.assertTrue(prs.mergeable_now(after.payload))
        self.assertGreater(after.rev, item.rev, "the card must be redrawn to show Merge")
        rev = after.rev
        prs.sync(self.store, gh=gh)
        self.assertEqual(self.store.get(item.id).rev, rev, "an unchanged PR does not redraw")

    def test_a_decided_item_keeps_what_andy_saw(self):
        gh = FakeGh([pr()])
        [item] = prs.sync(self.store, gh=gh)[0]
        self.store.decide(item.id, st.APPROVE, actor="t", data={"approved_sha": SHA})
        gh.prs[212] = pr(sha=NEW_SHA)
        prs.sync(self.store, gh=gh)
        self.assertEqual(self.store.get(item.id).payload["head_sha"], SHA)

    def test_a_failed_listing_raises(self):
        with self.assertRaises(RuntimeError):
            prs.sync(self.store, gh=FakeGh(fail={"pr list"}))


class TestApply(StoreCase):
    def decided(self, gh, verb, approved=SHA):
        [item] = prs.sync(self.store, gh=gh)[0]
        self.store.decide(item.id, verb, actor="t", data={"approved_sha": approved} if approved else None)
        return self.store.get(item.id)

    def test_merge_readies_the_draft_and_squashes_the_approved_commit(self):
        gh = FakeGh([pr()])
        ok, result = prs.apply(self.decided(gh, st.APPROVE), gh=gh, slug_for=self.slug)
        self.assertTrue(ok, result)
        self.assertEqual(result, "https://github.com/andy-herman/neural-bridge/pull/212")
        self.assertEqual(gh.verbs()[-4:], ["pr view", "pr ready", "pr merge", "issue edit"])
        merge = gh.calls[-2]
        self.assertEqual(merge[merge.index("--match-head-commit") + 1], SHA)
        self.assertIn("--squash", merge)
        subject = merge[merge.index("--subject") + 1]
        body = merge[merge.index("--body") + 1]
        self.assertEqual(subject, "fix: retry the flush on a locked queue (#212)")
        self.assertIn("Closes #88.", body)
        for leak in ("Co-Authored-By", "Claude", "Generated with"):
            self.assertNotIn(leak, subject + body)
        self.assertEqual(gh.calls[-1][-4:], ["--remove-label", "agent-review", "--add-label", "agent-done"])

    def test_nothing_merges_if_the_head_moved_or_the_checks_are_not_green(self):
        gh = FakeGh([pr()])
        item = self.decided(gh, st.APPROVE)
        gh.prs[212] = pr(sha=NEW_SHA)
        ok, result = prs.apply(item, gh=gh, slug_for=self.slug)
        self.assertFalse(ok)
        self.assertIn("new commits", result)
        gh.prs[212] = pr(checks=[{"status": "COMPLETED", "conclusion": "FAILURE"}])
        ok, result = prs.apply(item, gh=gh, slug_for=self.slug)
        self.assertFalse(ok)
        self.assertIn("failing", result)
        gh.prs[212] = pr(state="MERGED")
        self.assertFalse(prs.apply(item, gh=gh, slug_for=self.slug)[0])
        self.assertNotIn("pr merge", gh.verbs())

    def test_an_approval_without_a_recorded_commit_refuses(self):
        gh = FakeGh([pr()])
        ok, result = prs.apply(self.decided(gh, st.APPROVE, approved=None), gh=gh, slug_for=self.slug)
        self.assertFalse(ok)
        self.assertNotIn("pr merge", gh.verbs())

    def test_close_closes_the_pr_and_blocks_the_issue(self):
        gh = FakeGh([pr()])
        ok, result = prs.apply(self.decided(gh, st.REJECT, approved=None), gh=gh, slug_for=self.slug)
        self.assertTrue(ok, result)
        self.assertEqual(gh.verbs()[-2:], ["pr close", "issue edit"])
        self.assertEqual(gh.calls[-1][-4:], ["--remove-label", "agent-review", "--add-label", "agent-blocked"])
        self.assertIn("agent-blocked", result)

    def test_a_refused_merge_is_reported(self):
        gh = FakeGh([pr()], fail={"pr merge"})
        ok, result = prs.apply(self.decided(gh, st.APPROVE), gh=gh, slug_for=self.slug)
        self.assertFalse(ok)
        self.assertIn("pr merge refused", result)


if __name__ == "__main__":
    unittest.main()
