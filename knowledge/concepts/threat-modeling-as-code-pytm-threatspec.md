---
slug: threat-modeling-as-code-pytm-threatspec
verdict: PROMOTE
reason: concrete DevSecOps concept (threat-as-code with pytm/threatspec) grounded in INFO 310A curriculum work; reusable and well-scoped
checks_triggered: []
compiled_at: 2026-05-10T15:26:25Z
compiler_version: "1.2"
sources:
  - agent: teaching-prep
    session_id: 31c340be-065d-b8bb-87b6-e0aa430d890e
    transcript_sha256: 31c340be065db8bb87b6e0aa430d890e9b4b45de0b64accc0cda325651514017
    source_log: daily-logs/teaching-prep/2026-05-10.md
    session_n: 1
---

# threat-modeling-as-code-pytm-threatspec

Threat modeling as code treats a system's threat model as a versioned artifact — expressed in Python (pytm) or inline annotations (threatspec) — so it evolves alongside the source code it describes.

## Why this matters

Traditional threat modeling produces documents: PDFs, Visio diagrams, spreadsheets that drift out of sync with the actual system within weeks of being written. The as-code pattern moves the model into the repository, making it diffable, reviewable, and executable. When a developer adds a new data flow, the threat model update is part of the same pull request.

For INFO 310A — a course on information assurance and cybersecurity — this concept bridges the gap between textbook STRIDE/DREAD theory and the tooling students will encounter in professional DevSecOps environments. It surfaced as a cross-cutting candidate because it appears across both lecture material and lab exercises, relevant any time system design or secure-by-design principles come up.

## Key points

- pytm generates threat model diagrams and finding reports from a Python script; the script is the source of truth.
- threatspec embeds threat annotations directly in source code comments, keeping threat claims co-located with the code they describe.
- Both approaches make "update the threat model" a normal code-review activity rather than a separate security audit event.
- CI integration is the practical payoff: threat reports can be generated automatically on each build.

## How we use it

This concept was extracted during a Stage 2/3 calibration pass over INFO 310A corpus dossiers (lectures and labs). It earned a filing-gate promotion as a cross-cutting concept, meaning it plausibly recurs across multiple units rather than belonging to a single lecture.

## Related concepts

[[devSecOps-shift-left]] · [[stride-threat-modeling]] · [[secure-design-principles]]
