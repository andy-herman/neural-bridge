---
slug: subnet-math-cheat-sheet-for-midterm
verdict: PROMOTE
reason: concrete reusable pedagogical pattern grounded in teaching-prep context; slug and summary are coherent and concept-worthy
checks_triggered: []
compiled_at: 2026-05-10T15:25:55Z
compiler_version: "1.2"
sources:
  - agent: teaching-prep
    session_id: 31c340be-065d-b8bb-87b6-e0aa430d890e
    transcript_sha256: 31c340be065db8bb87b6e0aa430d890e9b4b45de0b64accc0cda325651514017
    source_log: daily-logs/teaching-prep/2026-05-10.md
    session_n: 1
---

# subnet-math-cheat-sheet-for-midterm

The pattern of supplying subnet calculation formulas on an exam sheet so students can demonstrate network design reasoning without being penalized for arithmetic they haven't memorized.

## Why this matters

Subnet math involves a small cluster of calculations — host counts per block, usable address ranges, network/broadcast boundary derivation — that are genuinely non-obvious on first exposure but purely mechanical once understood. Withholding those formulas on an exam tests memorization, not the skill the course is trying to assess: whether a student can decompose an addressing problem and apply the right tool.

Separating "can recall the formula" from "can apply it correctly" is a design decision that shapes what assessment actually measures. When teaching-prep builds exam materials for INFO 310A, that decision needs to be explicit and consistent across assessments, not re-litigated each time.

## Key points

- The pattern applies to calculations that are subtle enough that even working practitioners look them up — not as a convenience, but because the formula is the right level of abstraction.
- A cheat sheet signals to students what the course considers foundational knowledge versus reference knowledge.
- The decision to include a formula on a cheat sheet is itself a calibration judgment: does getting this wrong on an exam reveal a misconception, or just a memory gap?
- This pattern emerged during the Stage 2/3 calibration pass on INFO 310A, where it surfaced across multiple lecture and lab corpus files as a recurring design consideration.

## How we use it

The teaching-prep agent surfaced this concept during a pass over 11 corpus dossier files (lectures + labs) that yielded approximately 70 cross-cutting concept candidates. The pattern's recurrence across multiple files — not just one lecture — is what pushed it through filing-gate evaluation as a concept worth indexing, rather than a one-off design note.

## Related concepts

`[[cidr-as-cloud-lingua-franca]]`
`[[cve-cwe-owasp-hierarchy]]`
