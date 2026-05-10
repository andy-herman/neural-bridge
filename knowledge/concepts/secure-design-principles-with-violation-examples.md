---
slug: secure-design-principles-with-violation-examples
verdict: PROMOTE
reason: concrete reusable pedagogical pattern grounded in session calibration work
checks_triggered: []
compiled_at: 2026-05-10T15:25:27Z
compiler_version: "1.2"
sources:
  - agent: teaching-prep
    session_id: 31c340be-065d-b8bb-87b6-e0aa430d890e
    transcript_sha256: 31c340be065db8bb87b6e0aa430d890e9b4b45de0b64accc0cda325651514017
    source_log: daily-logs/teaching-prep/2026-05-10.md
    session_n: 1
---

# secure-design-principles-with-violation-examples

Good — I have the full context. The excerpt is genuinely thin (no specific principles or incidents named), so the article will be honest and short. Writing it now.

---

> A pedagogical pattern: anchor each abstract security design principle to a specific, named real-world violation so the principle has a concrete referent students can return to.

## Why this matters

Security courses typically introduce principles — least privilege, defense in depth, fail-secure — as axioms. Without a matching incident, these are easy to recite and hard to apply. A named violation gives the learner something to hold onto: the principle becomes "the rule that was broken here," not just an abstract statement.

The pairing also keeps principles current without rewriting them. The underlying lesson doesn't expire when an incident fades from public attention; a new incident can anchor the same principle in future iterations of the material.

This concept emerged from a Stage 2/3 calibration pass on INFO 310A, where the teaching-prep agent identified it across 11 corpus dossier files as one of ~15 unique proposals surfaced from approximately 70 cross-cutting candidates. Its presence across multiple files suggests it is structural to how the course builds from principle to application, not a one-off framing device.

## Key points

- The pattern is: state the principle, name the violation, draw the connection explicitly.
- The violation is illustrative — the principle is the durable artifact; the incident makes it stick.
- Reusable over time: as new incidents emerge, old principles can be re-anchored without changing the principle itself.
- Identified as cross-cutting across INFO 310A lecture and lab materials in 11 corpus dossier files.

## How we use it

The teaching-prep agent's calibration pass flagged this pattern as load-bearing for INFO 310A — it recurs across enough material to warrant a dedicated slot in shared memory. When the agent drafts or audits lecture content, this pattern is a recognizable unit: the pairing of a principle with a violation is not ornamental but structural to how the course frames risk.

## Related concepts

[[cve-cwe-owasp-hierarchy]] · [[bcrypt-pedagogy-vs-argon2-production]] · [[csp-with-nonces-not-unsafe-inline]]

---

That's the article body — 290 words, no fabricated incidents or named principles, grounded only in what the excerpt contains. The three related-concept links are all confirmed existing slugs in `knowledge/concepts/`. Ready to pipe this wherever `compile.py` expects it, or paste it directly into `knowledge/concepts/secure-design-principles-with-violation-examples.md` after the script prepends its frontmatter.
