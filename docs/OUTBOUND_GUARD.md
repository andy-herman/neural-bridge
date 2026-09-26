# Outbound guard

**Status: built 2026-09-25 (`scripts/outbound_guard.py`).** It takes effect on
the wiki route with the next compile run, and on the GitHub route when the
Discord daemon restarts and builds its first index.

Neural Bridge publishes agent-written text in two public places: the wiki
under `knowledge/`, which is tracked in this repo and written by
`scripts/compile.py`, and GitHub, where the Discord daemon opens issues,
comments, and pushes PR branches. Agents read the Obsidian vault, and some
vault notes carry confidentiality markings. The filing gate screens for prompt
injection and memory poisoning, not confidentiality. Until this guard, nothing
screened outbound text for marked material. A read-only leak check on
2026-09-25 found no leak; the guard exists to keep it that way.

## What it blocks

The method is the vault leak check's, reused unchanged:

1. Every vault note that the corpus gate marks is cut into 12-word shingles.
   The gate is Gemma GRC's `gate_note()`: non-public classification labels,
   private or confidential flags and tags, and handling banners. Notes under
   private policy folders are added to the marked set too.
2. Shingles that also occur in unmarked notes are dropped as shared
   boilerplate. What remains is the distinctive set.
3. An outbound item is blocked when it shares **3 or more** distinctive
   shingle occurrences with that set, which takes a verbatim run of about 14
   words. It is also blocked when it contains any private marking phrase.
   Matching ignores case, punctuation, markdown and line breaks, so
   reformatting a passage does not hide it.
4. **Fail closed.** A missing index blocks everything. So does a stale one
   (older than 24 hours), a corrupt one, or one built with a different hash
   key.

### Validation, 2026-09-25, real vault, counts only

| Control | Result |
|---|---|
| 20-word excerpts from marked notes, planted in filler | 30/30 blocked |
| 40-word excerpts | 29/30 blocked |
| 14-word excerpts (the floor) | 28/30 blocked |
| 13-word excerpts (below the floor) | 0/30 blocked |
| Whole marked notes | all blocked |
| Every marking phrase, in four formats each | all blocked |
| 20-word excerpts from unmarked notes | 0/200 blocked |
| Every tracked text file in this repo | 0/296 blocked |

The few misses at 14 and 40 words fall in text that also appears verbatim in
an unmarked note. The method discounts that text by design (0.8% of marked
shingles). Across 600 further random excerpts, one missed, for that reason.
A check takes about 2 ms for an issue body and about 120 ms for a 35,000-word
PR.

### What it does not catch

- Paraphrase, and verbatim runs shorter than about 14 words.
- Text that also appears verbatim in an unmarked note (see above).
- A note marked since the last index rebuild (at most an hour on the daemon's
  schedule).
- Anything outside the two routes: Discord and Telegram messages, the loop
  engineer (not installed; it pushes through its own code), and anything
  pushed by hand.
- Binary files in a push.

## Where it runs

| Route | Code | What is screened |
|---|---|---|
| Wiki | `compile.py` | Each concept, quarantine and connection file, and the new lines of `log.md` and `index.md`. Dry runs screen the same text, so they preview what a live run would block. |
| GitHub | `github_client.py` | Issue title and body, comments, closing comments, issue body edits. `/triage` now posts its comment through here too. |
| GitHub | `pr_proposals.py` | The whole proposal (branch, commit message, title, body, file paths and contents) when it is staged, again before execution touches the working tree, and finally everything the push would publish (every unpushed commit) plus the PR text, between the commit and `git push`. |
| GitHub | `agent_builder.py` | Every agent-supplied field before any git or file change, then the push itself, between the commit and `git push`. |

When something is blocked, nothing is published:

- **compile:** The candidate is not written, not archived over an existing
  concept, and not marked compiled. It is logged by source session and
  digest, never by slug or text. A marked line in `log.md` becomes a
  digest-only placeholder.
- **compile, guard unusable:** The whole run exits 1 before any model call,
  writes nothing, and leaves `last_run_at` alone. Those logs are then compiled
  on the next healthy run instead of being skipped.
- **GitHub:** The gh call never runs, and the Discord reply carries the
  counts. A refused push keeps the local branch, unpushed, for inspection.

## Where things live

| What | Where | Notes |
|---|---|---|
| Private policy | `~/.config/neural-bridge/outbound-guard.json` | Mode 600, outside the repo, never committed. Holds `marking_phrases` and `policy_folders`. Read only when the index is built. |
| Hash key | `~/.config/neural-bridge/outbound-guard.key` | Mode 600. Created on the first build. |
| Index | `data/outbound_guard/index.bin` | Gitignored, mode 600. Keyed blake2b 8-byte digests and counts; no text. |
| Audit log | `data/outbound_guard/audit.jsonl` | One line per check: surface, verdict, counts, digest. No text. |
| Corpus gate | `~/Development/gemma-grc/scripts/vault_ingest.py` | `gate_note()`, `read_note()`, `clean()`. |

The marking phrases and policy folders are kept out of this repo on purpose.
Naming them here would itself disclose what kind of material the vault holds.
The digests are keyed for the same reason: marking phrases are a few common
words, so unkeyed digests of them could be reversed by guessing.

Environment overrides, used by the tests: `NB_OUTBOUND_GUARD_DIR` (index and
audit log), `NB_OUTBOUND_GUARD_POLICY` (policy file; the key sits beside it),
`NB_OUTBOUND_GUARD_VAULT`, and `NB_OUTBOUND_GUARD_GATE`.

Policy file shape (placeholder values):

```json
{
  "marking_phrases": ["Example Corp Internal", "example-restricted"],
  "policy_folders": ["Some Contract Folder"]
}
```

## Operations

- **Refresh.** The daemon rebuilds the index at startup and then hourly, in a
  child process (about 6 seconds). `compile.py` rebuilds any index older than
  an hour before it runs. A failed rebuild is logged, and the last good index
  is used until it ages out, after which everything is refused.
- **Rebuild by hand:** `.venv/bin/python scripts/outbound_guard.py build`
- **Health:** `.venv/bin/python scripts/outbound_guard.py status`
- **Screen a draft before posting it yourself:**
  `pbpaste | .venv/bin/python scripts/outbound_guard.py check`
- **After a block:** the message reads `outbound guard blocked this text: N
  shingle(s) from M marked note(s) (categories), K marking phrase(s) [ref
  ...]`, and the ref matches a line in the audit log. Rewrite without the
  marked passage. If it is a false positive, publish by hand after checking.
- **Changing the policy:** edit the JSON, then rebuild.
- **Rotating the key:** delete the key file, then rebuild. Until the rebuild,
  the old index fails closed.

## Tests

- `scripts/test_outbound_guard.py`: positive and negative controls
  (40/20/14-word excerpts block, 13 words passes, boilerplate is discounted,
  marking phrases block in any format), every fail-closed case, and checks
  that no index file, audit line or message carries text. It also covers the
  CLI, push screening against a real temporary git remote, and Gemma GRC's
  real corpus gate on a synthetic vault (skipped when gemma-grc is absent).
- `scripts/discord_bot/test_outbound_guard_wiring.py`: every daemon GitHub
  path, and the refresh loop.
- `scripts/test_compile.py` (`TestOutboundGuardWiring`): every write under
  `knowledge/`, and the refusal to run with an unusable guard.

Every fixture is invented (`scripts/outbound_guard_testing.py`), because this
repo is public. Test modules that reach guarded code install a synthetic index
in `setUpModule`, so no test reads the real index or writes the real audit
log.
