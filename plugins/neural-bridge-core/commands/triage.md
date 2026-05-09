---
description: Run a senior-PM triage pass over open issues and PRs on andy-herman/neural-bridge. Reads `@senior-pm` directives from issue comments, surfaces them with proposed actions, and applies safe state changes (rename, close, label, AC append) after confirmation. Risky actions (PR merges, body rewrites, force-pushes) are always surfaced for explicit approval.
---

Use the senior-pm specialist agent (`plugins/neural-bridge-core/agents/senior-pm.md`) to triage the project board.

## Steps

1. **List open issues and PRs.**
   ```
   gh issue list -R andy-herman/neural-bridge --state open --json number,title,labels,updatedAt
   gh pr list -R andy-herman/neural-bridge --state open --json number,title,reviews,mergeable,updatedAt
   ```

2. **Scan comments for `@senior-pm` directives.** For each issue, fetch comments:
   ```
   gh issue view <N> -R andy-herman/neural-bridge --comments --json comments
   ```
   Look for comments starting with `@senior-pm`. Common directive shapes:

   | Directive | Action |
   |---|---|
   | `@senior-pm close — <reason>` | Close with the reason as a closing comment |
   | `@senior-pm rename: <new title>` | Edit title |
   | `@senior-pm add label <label>` | Add label |
   | `@senior-pm remove label <label>` | Remove label |
   | `@senior-pm block on #<N>` | Add `blocked` label, append depends-on note to body |
   | `@senior-pm add ac: <text>` | Append `**Done when:** <text>` to body |
   | `@senior-pm note: <text>` | Add as a regular comment, no state change |

3. **Categorize each found directive:**
   - **Safe** — rename, close, label change, AC append, dependency note. Propose to apply.
   - **Unclear** — free-form note, ambiguous wording, malformed directive. Surface for human review.
   - **Risky** — PR merges, body rewrites, force-pushes, deletions, branch ops. Never auto-apply; quote the directive and ask for explicit confirmation.

4. **Report before applying.** Output a structured plan:
   ```
   Issue #N (title)
     Directive: @senior-pm <text>
     Proposed: <action>
     Category: safe|unclear|risky
   ```
   Wait for the user to approve all, approve some, or skip before mutating any state.

5. **Apply approved actions.** Use these gh commands:
   - `gh issue edit <N> -R andy-herman/neural-bridge --title "<new>"`
   - `gh issue edit <N> -R andy-herman/neural-bridge --body-file <path>`
   - `gh issue edit <N> -R andy-herman/neural-bridge --add-label "<label>"`
   - `gh issue close <N> -R andy-herman/neural-bridge`
   - `gh issue comment <N> -R andy-herman/neural-bridge --body "<text>"`

6. **Write a triage log** at `knowledge/agents/senior-pm/<YYYY-MM-DD>-triage.md`:
   ```markdown
   ---
   type: triage-log
   date: <YYYY-MM-DD>
   ---

   # Triage <YYYY-MM-DD>

   ## Scope
   - Issues scanned: <N>
   - PRs scanned: <N>

   ## Directives processed
   - #N — <directive> — applied|deferred|skipped (reason)

   ## Recommendations beyond directives
   - <free-text recommendations from the senior-pm audit shape>

   ## Open follow-ups
   - <items the user needs to act on personally>
   ```

7. **Confirm completion** to the user with a one-line summary (X applied, Y deferred, Z surfaced).

## Tooling notes

- gh binary on Windows: `C:\Program Files\GitHub CLI\gh.exe`. From Bash: `export PATH="/c/Program Files/GitHub CLI:$PATH"`.
- For `gh api` endpoints, drop the leading slash from the URL path (Git Bash on Windows rewrites paths starting with `/` as filesystem paths).
- When writing JSON payloads for `gh api --input`, use bash heredocs (PowerShell 5.1's `Out-File -Encoding utf8` writes a BOM that the API rejects).

## Don't

- Don't auto-apply risky actions (PR merges, body rewrites, force-pushes, deletions). Always surface and wait.
- Don't process the same directive twice. Triage logs in `knowledge/agents/senior-pm/` are the deduplication record. If a directive was applied in a prior triage, skip it (with a one-line note).
- Don't fire on directives older than 30 days. Stale directives may have been resolved out-of-band.
- Don't write to `knowledge/concepts/` or other agents' subdirs. Senior-pm writes only to `knowledge/agents/senior-pm/`.
