---
slug: bcrypt-pedagogy-vs-argon2-production
verdict: PROMOTE
reason: concrete reusable pedagogical pattern grounded in curriculum design; slug and summary are coherent and well-formed
checks_triggered: []
compiled_at: 2026-05-10T15:20:16Z
compiler_version: "1.2"
sources:
  - agent: teaching-prep
    session_id: 31c340be-065d-b8bb-87b6-e0aa430d890e
    transcript_sha256: 31c340be065db8bb87b6e0aa430d890e9b4b45de0b64accc0cda325651514017
    source_log: daily-logs/teaching-prep/2026-05-10.md
    session_n: 1
---

# bcrypt-pedagogy-vs-argon2-production

Teaching the simpler, legacy password-hashing algorithm (bcrypt) in hands-on lab exercises while explicitly naming the current production recommendation (Argon2id) in lecture — a pattern that surfaces whenever pedagogical accessibility and current best practice diverge in a security course.

## Why this matters

INFO 310A covers password storage in a context where students need working, debuggable code quickly. bcrypt has wider library support, shorter setup time, and decades of tutorials, which makes it a practical lab vehicle. Argon2id is the OWASP-current recommendation and what students should reach for in real deployments. Using bcrypt without acknowledgment would leave students with an incomplete picture; abandoning it for Argon2id in every lab would slow down exercises that are really about threat modeling or authentication flow, not hashing internals.

The pattern generalizes. Any course where the field has moved faster than the teaching corpus will produce moments where the example in the lab and the recommendation in lecture intentionally differ. Naming this as a deliberate choice, rather than an oversight, is the practice worth capturing.

## Key points

- bcrypt is used in labs because it reduces setup friction, not because it is the preferred algorithm.
- Argon2id (Argon2 variant recommended by OWASP and RFC 9106) is introduced in lecture as the production target.
- The gap between lab example and lecture recommendation is intentional and should be stated explicitly to students, not left implicit.
- This pattern applies to any INFO 310A topic where the corpus dossier files (lectures and labs) reflect different points in time or different audiences.
- The concept was one of 15 unique proposals extracted across 11 corpus dossier files during a Stage 2/3 calibration pass on INFO 310A.

## How we use it

The `teaching-prep` agent surfaced this concept while reviewing INFO 310A lectures and labs together. When the calibration pass compares lecture content against lab scaffolding, it flags cases where the two layers use different tools or algorithms. This concept gives that flag a name so future passes can recognize the pattern without treating every bcrypt occurrence as an error.

## Related concepts

`[[pedagogical-divergence-from-production]]`  
`[[owasp-recommendation-lag-in-curriculum]]`  
`[[lab-vs-lecture-split]]`
