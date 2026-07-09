# Branch Triage Report, 2026-07-09

Read-only triage of all 19 unmerged remote branches on origin against `main` (tip `e53227f`). Method: `git rev-list --count` for ahead/behind, `git cherry` for patch-equivalence (squash merges leave branches "ahead" even when their content shipped), three-dot diffs for scope, and targeted content diffs against current main for the ambiguous cases.

Big picture: this repo squash-merges PRs and never deletes branches. 15 of 19 branches are fully contained in main and are pure cleanup. Only 3 branches carry live, unmerged work, and 2 of those (the compile-pipeline pair) are a stacked series that must land in order.

## Summary table

| Branch | Ahead/Behind | Scope | Verdict | Reason |
|---|---|---|---|---|
| feat/multi-vote-filing-gate | 3/1 | ~1070 lines, compile.py + eval harnesses | MERGE-CANDIDATE | Active (Jun 26), only 1 docs commit behind, no file overlap with main's delta |
| feat/provider-fallback-shim | 3/1 | ~1300 lines, compile.py + model_invoke.py | NEEDS-REBASE | Valuable resilience work, but stacked on multi-vote's first commit; conflicts with it in compile.py and test_compile.py |
| feat/proactive-surface-on-relevance | 2/28 | ~900 lines, semantic_search + handlers | NEEDS-REBASE | Commit c34e710 (closes #124) never merged; handlers.py moved 366 lines on main since branch point |
| automation/post-pr-checkout-main | 1/15 | daemon + charters + tests | CLOSE | Squash-merged as #147 (b99b887); patch-equivalent |
| chore/agent-memory-pyramid-asset | 1/27 | 2 docs files | CLOSE | Squash-merged as #131 (30041c4) |
| copilot/docs-sop-document-post-pr-git-checkout-main | 2/30 | 7 lines across 5 charter/prompt files | SUPERSEDED | Main carries fuller equivalents of every hygiene passage via #147 plus the em-dash sweep (#140) |
| copilot/v2-first-public-blog-post | 3/30 | 1 draft file, 88 lines | CLOSE | Draft merged as #128; branch-tip file is byte-identical on main |
| echo/synapse-ingester | 1/7 | 1 script, 190 lines | CLOSE | Squash-merged as #150 (2cf3e6e) |
| feat/conversation-log-vault-archive | 1/31 | ~480 lines | CLOSE | Squash-merged as #120 (ec421da) |
| feat/cross-agent-shared-memory | 1/30 | ~230 lines | CLOSE | Squash-merged as #127 (edc6b15) |
| feat/expand-push-allowlist-and-history | 1/34 | 9 lines, 3 files | CLOSE | Squash-merged as #118 (4734fd0) |
| feat/ingest-pptx-xlsx | 1/35 | ~360 lines | CLOSE | Squash-merged as #117 (4a8de01) |
| feat/project-board-auto-add | 1/35 | 1 workflow file | CLOSE | Squash-merged as #116 (b1c37b7) |
| feat/semantic-search-via-ollama-bge-m3 | 1/28 | ~700 lines | CLOSE | Squash-merged as #129 (34905ba); also the base of proactive-surface |
| feat/session-resumption-per-channel-agent | 1/34 | ~480 lines | CLOSE | Squash-merged as #119 (050e1ff) |
| feat/weekly-lessons-learned-summarization | 1/30 | ~615 lines | CLOSE | Squash-merged as #125 (b419a35) |
| fix/attribution-karpathy-spelling | 1/15 | 1 line | CLOSE | Squash-merged as #148 (12263b3) |
| honcho-and-luna-telegram | 2/6 | ~700 lines | CLOSE | Squash-merged as #151 (67f1cc4); zero branch-only lines vs main |
| loid-agent | 1/5 | ~820 lines | CLOSE | Squash-merged as #152 (cee93ec) |

## Active branches

### feat/multi-vote-filing-gate (MERGE-CANDIDATE)

Commits 5b3af9c, 7324037, 9700161 (Jun 24-26). Adds a multi-vote filing gate to `scripts/compile.py`, retry logic for transient gate passes, a calibration case set (`scripts/eval/filing_gate_cases.jsonl`), and two eval harnesses (`scripts/eval_filing_gate.py`, `scripts/eval_stability.py`) with tests. The stability harness includes a preflight fail-fast that was informed by a real live-run failure (expired CLI auth mid-batch), so this has been exercised, not just written.

Conflict risk: low. The only commit on main since the branch point is e53227f, a docs truth pass that touches `scripts/lint.py`, README/AGENTS/STATUS, and marketplace.json. Zero file overlap with this branch. Merge first, before the fallback shim.

### feat/provider-fallback-shim (NEEDS-REBASE)

Commits 5b3af9c (shared with multi-vote), 70fd93c, 173da30 (Jun 24). Adds `scripts/model_invoke.py` with an OpenAI-compatible fallback for the compile pipeline's text-only LLM calls, env-gated and dependency-free, plus a real NameError fix in compile.py (run_log_path computed after the Discord summary block that references it). The commit message says it stacks on the multi-vote PR, but it forked after multi-vote's first commit only, so it lacks 7324037 and 9700161.

Conflict risk: certain conflicts with multi-vote-filing-gate in `scripts/compile.py` and `scripts/test_compile.py`, plus divergent `filing_gate_cases.jsonl` (22 vs 15 cases) and duplicated `eval_filing_gate.py`. Rebase onto main after multi-vote lands; the rebase should reduce to its two unique commits. Note the 173da30 NameError fix is a production bug fix worth cherry-picking early if the rebase stalls.

### feat/proactive-surface-on-relevance (NEEDS-REBASE)

Two commits: aa2b7be (already on main as #129) and c34e710, which closes #124 and is NOT on main. It completes the four-layer memory roadmap: at mention-prompt-build time the daemon embeds the incoming message, retrieves semantically relevant prior turns from the archive, and prepends them under a "Possibly relevant prior turns" header. Main has no trace of this (no proactive-surface code in handlers.py or semantic_search.py).

Conflict risk: moderate to high in `scripts/discord_bot/handlers.py`, which has grown 366 lines on main since the branch point (squad-discuss, honcho wiring, fleet heartbeats). Good news: main's `semantic_search.py` is byte-identical to the branch's base version, so the semantic_search.py and test_semantic_search.py hunks of c34e710 should apply cleanly. Recommended path: cherry-pick c34e710 onto a fresh branch off main and hand-resolve the handlers.py hunks.

## Superseded

### copilot/docs-sop-document-post-pr-git-checkout-main (SUPERSEDED)

Copilot-authored docs pass adding "Post-PR branch hygiene" notes to four agent charters and the mention prompt. Main already carries equivalent and more detailed versions of every passage (shipped with #147's charter updates, then reworded by the em-dash sweep in #140). Verified by grep: all five files on main reference the auto-checkout behavior and the `Branch hygiene.md` SOP. Nothing to salvage. Close and delete.

## Closed / merged (delete remotely)

All of the following are patch-equivalent to main per `git cherry` (squash merges), with the PR number that landed them: automation/post-pr-checkout-main (#147), chore/agent-memory-pyramid-asset (#131), copilot/v2-first-public-blog-post (#128, branch-tip draft byte-identical on main), echo/synapse-ingester (#150), feat/conversation-log-vault-archive (#120), feat/cross-agent-shared-memory (#127), feat/expand-push-allowlist-and-history (#118), feat/ingest-pptx-xlsx (#117), feat/project-board-auto-add (#116), feat/semantic-search-via-ollama-bge-m3 (#129), feat/session-resumption-per-channel-agent (#119), feat/weekly-lessons-learned-summarization (#125), fix/attribution-karpathy-spelling (#148), honcho-and-luna-telegram (#151, zero branch-only lines), loid-agent (#152).

Safe to delete with `git push origin --delete <branch>` once someone confirms no open PRs still point at them. Consider enabling GitHub's "Automatically delete head branches" repo setting so this list stops growing.
