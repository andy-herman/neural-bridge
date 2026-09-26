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

The method is the vault leak check's, with one deliberate change:

1. Every vault note that the corpus gate marks is cut into 12-word shingles.
   The gate is Gemma GRC's `gate_note()`: non-public classification labels,
   private or confidential flags and tags, and handling banners. Notes under
   private policy folders are added to the marked set too. Both the raw
   markdown and the cleaned text are indexed, so a quote copied with its
   links still matches.
2. An outbound item is blocked when it shares **3 or more** shingle
   occurrences with that set, which takes a verbatim run of about 14 words.
   It is also blocked when it contains any private marking phrase. Matching
   ignores case, punctuation, markdown and line breaks, so reformatting a
   passage does not hide it.
3. **Fail closed.** A missing index blocks everything. So does a stale one
   (older than 24 hours), a corrupt one, or one built with a different hash
   key.

**The change: no discount.** The leak check dropped shingles that also occur
in unmarked notes, treating them as shared boilerplate. That is safe for a
one-off audit and unsafe for a live guard. Agents write unmarked notes all the
time (conversation archives, session notes), so one quote of a marked passage
would unprotect it at the next hourly rebuild. Measured on 2026-09-25, the
discount covered 789 shingles, three quarters of them copies in AI-written
session notes. It prevented no false positive on this repo or on the
published blog.

**What is exempt: text already published.** A shingle that already appears on
the default branch of this repo or of the blog is left out of the index.
Republishing it cannot leak anything new. Without the exemption, a public
citation that a marked note happens to repeat (a NIST document title and URL,
say) would block every later post that cites it. Only origin's committed
content counts, as each local clone's `origin/main` shows it; the working tree
and unpushed commits never do. Agents can only add to that content through
routes this guard screens, so the exemption cannot launder a quote the way the
discount could. Marking phrases are never exempt.

### Validation, 2026-09-25, real vault, counts only

| Control | Result |
|---|---|
| 40-word excerpts from marked notes, planted in filler | 30/30 blocked |
| 20-word excerpts | 30/30 blocked, and 10/10 for every gate category |
| 14-word excerpts (the floor) | 30/30 blocked |
| 13-word excerpts (below the floor) | 0/30 blocked |
| Whole marked notes | all blocked |
| Every marking phrase, in four formats each | all blocked |
| 20-word excerpts from unmarked notes | 0/200 blocked |
| Every tracked text file in this repo | 0/297 blocked |
| Every tracked text file in the published blog | 0/219 blocked |

A check takes about 2 ms for an issue body and about 120 ms for a 35,000-word
PR. A rebuild takes a few seconds.

A second pass on 2026-09-26 followed the policy gaining more folders. Before
the exemption, 4 of the 219 blog files were blocked: three on public NIST
citation lines that a marked note also holds, one on a 14-word published
phrase. After it, the results were:

| Control | Result |
|---|---|
| 20-word excerpts from each added folder | 10/10 blocked |
| 13-word excerpts from each added folder | 0/10 blocked |
| Every tracked text file in this repo | 0/301 blocked |
| Every tracked text file in the published blog | 0/219 blocked |

Only 13 shingles were exempt as already public.

### What it does not catch

- Paraphrase, and verbatim runs shorter than about 14 words.
- A note marked since the last index rebuild (at most an hour on the daemon's
  schedule).
- Anything outside the two routes: Discord and Telegram messages (except the
  compile summary, which carries the same screened lines as `log.md`), the
  loop engineer (not installed; it pushes through its own code), direct agent
  edits to tracked `knowledge/` files outside `concepts/` and `quarantine/`,
  and anything pushed by hand.

The opposite trade-off, from dropping the discount: public text that also
sits inside a marked note is blocked too, until it has been published once.
An example is a regulation paragraph or a citation that a private note also
quotes. The first time, rephrase, or check it and publish by hand; after
that, the exemption above covers it.

## Where it runs

| Route | Code | What is screened |
|---|---|---|
| Wiki | `compile.py` | Each concept, quarantine and connection file, and the new lines of `log.md` and `index.md`. The Discord run summary carries the same screened lines. Dry runs screen the same text, so they preview what a live run would block. |
| GitHub | `github_client.py` | Issue title and body, comments, closing comments, issue body edits. `/triage` now posts its comment through here too. |
| GitHub | `pr_proposals.py` | The whole proposal (branch, commit message, title, body, file paths and contents) when it is staged, and again before execution touches the working tree. Finally, between the commit and `git push`: everything the push to origin would publish (every unpushed commit, merges included, with `-diff` and binary attributes overridden) plus the PR text. It commits only its own paths. |
| GitHub | `agent_builder.py` | Every agent-supplied field before any git or file change, then the push itself, between the commit and `git push`. It stages and commits only the files it writes. |

When something is blocked, nothing is published:

- **compile:** The candidate is not written, not archived over an existing
  concept, and not marked compiled. It is logged by source session and
  digest, never by slug or text. A marked line in `log.md` becomes a
  digest-only placeholder.
- **compile, guard unusable:** The whole run exits 1 before any model call,
  writes nothing, and leaves `last_run_at` alone. Those logs are then compiled
  on the next healthy run instead of being skipped. If the guard fails
  partway through a run, the run stops: what already landed is kept, and
  `last_run_at` stays put.
- **GitHub:** The gh call never runs, and the Discord reply carries the
  counts. A refused push keeps the local branch, unpushed, for inspection.

## Where things live

| What | Where | Notes |
|---|---|---|
| Private policy | `~/.config/neural-bridge/outbound-guard.json` | Mode 600, outside the repo, never committed. Holds `marking_phrases` and `policy_folders`. Read only when the index is built. Folder matching ignores case, and a folder that matches no note fails the build. |
| Hash key | `~/.config/neural-bridge/outbound-guard.key` | Mode 600. Created on the first build. |
| Index | `data/outbound_guard/index.bin` | Gitignored, mode 600. Keyed blake2b 8-byte digests and counts; no text. |
| Audit log | `data/outbound_guard/audit.jsonl` | One line per check: surface, verdict, counts, digest. No text. |
| Corpus gate | `~/Development/gemma-grc/scripts/vault_ingest.py` | `gate_note()`, `read_note()`, `clean()`. |
| Public repos | this clone and `~/Development/neural-bridge-blog` | Text on each clone's `origin/main` is exempt. A clone or ref that cannot be read just means nothing from it is exempt. |

The marking phrases and policy folders are kept out of this repo on purpose.
Naming them here would itself disclose what kind of material the vault holds.
The digests are keyed for the same reason: marking phrases are a few common
words, so unkeyed digests of them could be reversed by guessing.

Environment overrides, used by the tests: `NB_OUTBOUND_GUARD_DIR` (index and
audit log), `NB_OUTBOUND_GUARD_POLICY` (policy file; the key sits beside it),
`NB_OUTBOUND_GUARD_VAULT`, `NB_OUTBOUND_GUARD_GATE`, and
`NB_OUTBOUND_GUARD_PUBLIC_REPOS` (clones separated by `:`; set but empty means
none).

Policy file shape (placeholder values):

```json
{
  "marking_phrases": ["Example Corp Internal", "example-restricted"],
  "policy_folders": ["Some Contract Folder"]
}
```

## Operations

- **Refresh.** The daemon rebuilds the index at startup and then hourly, in a
  child process (a few seconds). `compile.py` rebuilds any index older than
  an hour before it runs. A failed rebuild is logged, and the last good index
  is used until it ages out, after which everything is refused.
- **Exemptions follow the clones' origin refs as last fetched.** The
  auto-reload watcher fetches this repo every two minutes. The blog clone's
  `origin/main` only moves when that clone is fetched, so a newly published
  post counts as public from the next fetch and rebuild.
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

- `scripts/test_outbound_guard.py` covers:
  - Positive and negative controls: 40, 20 and 14-word excerpts block, 13
    words passes, and marking phrases block in any format.
  - The laundering case: an agent-written note quoting a marked passage must
    not unprotect it.
  - The public exemption: text on a public clone's origin branch is exempt,
    while local commits, working-tree edits, binary files, missing clones and
    marking phrases never are.
  - Raw-markdown quotes, and every fail-closed case.
  - That no index file, audit line or message carries text.
  - The CLI, and Gemma GRC's real corpus gate on a synthetic vault (skipped
    when gemma-grc is absent).
  - Push screening against a real temporary git remote: merges, a second
    remote, and `-diff` attributes.
- `scripts/discord_bot/test_outbound_guard_wiring.py`: every daemon GitHub
  path, the refresh loop, no echo of refused text, and `execute_proposal`
  end to end on a real temporary repo.
- `scripts/test_compile.py` (`TestOutboundGuardWiring`) covers:
  - Every write under `knowledge/`, and the Discord summary.
  - The refusal to run with an unusable guard, and the stop when the guard
    fails mid-run.

Every fixture is invented (`scripts/outbound_guard_testing.py`), because this
repo is public. Test modules that reach guarded code install a synthetic index
in `setUpModule`, so no test reads the real index or writes the real audit
log.
