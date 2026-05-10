---
type: log
created: 2026-05-08
tags: [neural-bridge, log]
---

# Wiki Activity Log

Append-only. New entries at the bottom. Prefix each entry with `## YYYY-MM-DD`.

## 2026-05-08

- V1 scaffold created.
- Wiki initialized with three agent subdirectories: `research/`, `teaching-prep/`, `content/`.
- No concept articles yet.
- Hooks not yet wired (V2).

## 2026-05-10

- compile complete: PROMOTE=2 QUARANTINE=0 REJECT=0 errors=0 skipped=0
  - PROMOTE-rich filing-gate-quarantine-vs-reject -> knowledge/concepts/filing-gate-quarantine-vs-reject.md
  - PROMOTE-rich dry-run-output-format -> knowledge/concepts/dry-run-output-format.md
- compile complete: PROMOTE=0 QUARANTINE=2 REJECT=0 errors=0 skipped=0
  - QUARANTINE filing-gate-quarantine-vs-reject -> knowledge/quarantine/filing-gate-quarantine-vs-reject.md (overclaim)
  - QUARANTINE dry-run-output-format -> knowledge/quarantine/dry-run-output-format.md (overclaim)
- compile complete: PROMOTE=2 QUARANTINE=0 REJECT=0 errors=0 skipped=0
  - PROMOTE-rich filing-gate-quarantine-vs-reject -> knowledge/concepts/filing-gate-quarantine-vs-reject.md
  - PROMOTE-rich dry-run-output-format -> knowledge/concepts/dry-run-output-format.md
- compile complete: PROMOTE=2 QUARANTINE=0 REJECT=0 errors=0 skipped=0
  - PROMOTE-rich filing-gate-quarantine-vs-reject -> knowledge/concepts/filing-gate-quarantine-vs-reject.md
  - PROMOTE-rich dry-run-output-format -> knowledge/concepts/dry-run-output-format.md
- compile complete: PROMOTE=0 QUARANTINE=2 REJECT=0 errors=0 skipped=0
  - QUARANTINE filing-gate-quarantine-vs-reject -> knowledge/quarantine/filing-gate-quarantine-vs-reject.md (overclaim)
  - QUARANTINE dry-run-output-format -> knowledge/quarantine/dry-run-output-format.md (overclaim)
- compile complete: PROMOTE=2 QUARANTINE=0 REJECT=0 errors=0 skipped=0
  - PROMOTE-rich filing-gate-quarantine-vs-reject -> knowledge/concepts/filing-gate-quarantine-vs-reject.md
  - PROMOTE-rich dry-run-output-format -> knowledge/concepts/dry-run-output-format.md
- compile complete: PROMOTE=2 QUARANTINE=0 REJECT=0 errors=0 skipped=0
  - PROMOTE-rich filing-gate-quarantine-vs-reject -> knowledge/concepts/filing-gate-quarantine-vs-reject.md
  - PROMOTE-rich dry-run-output-format -> knowledge/concepts/dry-run-output-format.md
- compile complete: PROMOTE=0 QUARANTINE=2 REJECT=0 errors=0 skipped=0
  - QUARANTINE filing-gate-quarantine-vs-reject -> knowledge/quarantine/filing-gate-quarantine-vs-reject.md (overclaim)
  - QUARANTINE dry-run-output-format -> knowledge/quarantine/dry-run-output-format.md (overclaim)
- compile complete: PROMOTE=2 QUARANTINE=0 REJECT=0 errors=0 skipped=0
  - PROMOTE-rich filing-gate-quarantine-vs-reject -> knowledge/concepts/filing-gate-quarantine-vs-reject.md
  - PROMOTE-rich dry-run-output-format -> knowledge/concepts/dry-run-output-format.md
- compile complete: PROMOTE=7 QUARANTINE=8 REJECT=0 errors=0 skipped=0 connections=21
  - QUARANTINE ai-enabled-social-engineering-canon -> knowledge/quarantine/ai-enabled-social-engineering-canon.md (summary references specific factual claim (Hong Kong $25M deepfake CFO incident) that is not present or supported in the session excerpt)
  - PROMOTE-rich bcrypt-pedagogy-vs-argon2-production -> knowledge/concepts/bcrypt-pedagogy-vs-argon2-production.md
  - PROMOTE-rich cidr-as-cloud-lingua-franca -> knowledge/concepts/cidr-as-cloud-lingua-franca.md
  - QUARANTINE cloud-native-firewalls-replace-appliances -> knowledge/quarantine/cloud-native-firewalls-replace-appliances.md (summary cites specific technologies (AWS SG, Azure NSG, GCP FR, eBPF microsegmentation) not present in the session excerpt — claims are untraceable to the provided evidence)
  - PROMOTE-rich csp-with-nonces-not-unsafe-inline -> knowledge/concepts/csp-with-nonces-not-unsafe-inline.md
  - PROMOTE-rich cve-cwe-owasp-hierarchy -> knowledge/concepts/cve-cwe-owasp-hierarchy.md
  - QUARANTINE defenders-can-win-stories -> knowledge/quarantine/defenders-can-win-stories.md (summary references specific operations (Endgame, Cronos, BlackCat) not mentioned or supported in the session excerpt)
  - QUARANTINE docker-up-doesnt-return -> knowledge/quarantine/docker-up-doesnt-return.md (summary references a specific output file path (research/common-student-questions.md) not mentioned in the session excerpt, and the excerpt contains no concrete session evidence of docker compose confusion — only a count of candidates extracted)
  - QUARANTINE hashing-vs-encryption-explicit-distinction -> knowledge/quarantine/hashing-vs-encryption-explicit-distinction.md (summary's specific pedagogical claims — 'recurring student confusion', 'one-way vs two-way framing', 'password example' — are not evidenced anywhere in the session excerpt, which only reports aggregate counts of candidates extracted)
  - QUARANTINE idor-still-a01-in-2025 -> knowledge/quarantine/idor-still-a01-in-2025.md (summary asserts specific facts (OWASP Top 10:2025 ranking, canonical lab content) that the session excerpt does not support — excerpt describes only a generic calibration pass with no mention of IDOR, BAC, or OWASP rankings)
  - QUARANTINE lockout-vs-credential-stuffing -> knowledge/quarantine/lockout-vs-credential-stuffing.md (summary makes specific security claims (lockout/brute-force, MFA/credential-stuffing distinction) not present in the excerpt, which is a meta-level calibration summary with no supporting content)
  - PROMOTE-rich secure-design-principles-with-violation-examples -> knowledge/concepts/secure-design-principles-with-violation-examples.md
  - PROMOTE-rich subnet-math-cheat-sheet-for-midterm -> knowledge/concepts/subnet-math-cheat-sheet-for-midterm.md
  - PROMOTE-rich threat-modeling-as-code-pytm-threatspec -> knowledge/concepts/threat-modeling-as-code-pytm-threatspec.md
  - QUARANTINE vdp-vs-bug-bounty-distinction -> knowledge/quarantine/vdp-vs-bug-bounty-distinction.md (summary makes specific factual claims (legal safe harbor framing, CISA federal mandate) that the session excerpt does not support — excerpt is a compilation meta-log, not the teaching session that would evidence those claims)
  - CONNECTION bcrypt-pedagogy-vs-argon2-production ↔ cidr-as-cloud-lingua-franca -> knowledge/connections/bcrypt-pedagogy-vs-argon2-production--cidr-as-cloud-lingua-franca.md
  - CONNECTION bcrypt-pedagogy-vs-argon2-production ↔ csp-with-nonces-not-unsafe-inline -> knowledge/connections/bcrypt-pedagogy-vs-argon2-production--csp-with-nonces-not-unsafe-inline.md
  - CONNECTION bcrypt-pedagogy-vs-argon2-production ↔ cve-cwe-owasp-hierarchy -> knowledge/connections/bcrypt-pedagogy-vs-argon2-production--cve-cwe-owasp-hierarchy.md
  - CONNECTION bcrypt-pedagogy-vs-argon2-production ↔ secure-design-principles-with-violation-examples -> knowledge/connections/bcrypt-pedagogy-vs-argon2-production--secure-design-principles-with-violation-examples.md
  - CONNECTION bcrypt-pedagogy-vs-argon2-production ↔ subnet-math-cheat-sheet-for-midterm -> knowledge/connections/bcrypt-pedagogy-vs-argon2-production--subnet-math-cheat-sheet-for-midterm.md
  - CONNECTION bcrypt-pedagogy-vs-argon2-production ↔ threat-modeling-as-code-pytm-threatspec -> knowledge/connections/bcrypt-pedagogy-vs-argon2-production--threat-modeling-as-code-pytm-threatspec.md
  - CONNECTION cidr-as-cloud-lingua-franca ↔ csp-with-nonces-not-unsafe-inline -> knowledge/connections/cidr-as-cloud-lingua-franca--csp-with-nonces-not-unsafe-inline.md
  - CONNECTION cidr-as-cloud-lingua-franca ↔ cve-cwe-owasp-hierarchy -> knowledge/connections/cidr-as-cloud-lingua-franca--cve-cwe-owasp-hierarchy.md
  - CONNECTION cidr-as-cloud-lingua-franca ↔ secure-design-principles-with-violation-examples -> knowledge/connections/cidr-as-cloud-lingua-franca--secure-design-principles-with-violation-examples.md
  - CONNECTION cidr-as-cloud-lingua-franca ↔ subnet-math-cheat-sheet-for-midterm -> knowledge/connections/cidr-as-cloud-lingua-franca--subnet-math-cheat-sheet-for-midterm.md
  - CONNECTION cidr-as-cloud-lingua-franca ↔ threat-modeling-as-code-pytm-threatspec -> knowledge/connections/cidr-as-cloud-lingua-franca--threat-modeling-as-code-pytm-threatspec.md
  - CONNECTION csp-with-nonces-not-unsafe-inline ↔ cve-cwe-owasp-hierarchy -> knowledge/connections/csp-with-nonces-not-unsafe-inline--cve-cwe-owasp-hierarchy.md
  - CONNECTION csp-with-nonces-not-unsafe-inline ↔ secure-design-principles-with-violation-examples -> knowledge/connections/csp-with-nonces-not-unsafe-inline--secure-design-principles-with-violation-examples.md
  - CONNECTION csp-with-nonces-not-unsafe-inline ↔ subnet-math-cheat-sheet-for-midterm -> knowledge/connections/csp-with-nonces-not-unsafe-inline--subnet-math-cheat-sheet-for-midterm.md
  - CONNECTION csp-with-nonces-not-unsafe-inline ↔ threat-modeling-as-code-pytm-threatspec -> knowledge/connections/csp-with-nonces-not-unsafe-inline--threat-modeling-as-code-pytm-threatspec.md
  - CONNECTION cve-cwe-owasp-hierarchy ↔ secure-design-principles-with-violation-examples -> knowledge/connections/cve-cwe-owasp-hierarchy--secure-design-principles-with-violation-examples.md
  - CONNECTION cve-cwe-owasp-hierarchy ↔ subnet-math-cheat-sheet-for-midterm -> knowledge/connections/cve-cwe-owasp-hierarchy--subnet-math-cheat-sheet-for-midterm.md
  - CONNECTION cve-cwe-owasp-hierarchy ↔ threat-modeling-as-code-pytm-threatspec -> knowledge/connections/cve-cwe-owasp-hierarchy--threat-modeling-as-code-pytm-threatspec.md
  - CONNECTION secure-design-principles-with-violation-examples ↔ subnet-math-cheat-sheet-for-midterm -> knowledge/connections/secure-design-principles-with-violation-examples--subnet-math-cheat-sheet-for-midterm.md
  - CONNECTION secure-design-principles-with-violation-examples ↔ threat-modeling-as-code-pytm-threatspec -> knowledge/connections/secure-design-principles-with-violation-examples--threat-modeling-as-code-pytm-threatspec.md
  - CONNECTION subnet-math-cheat-sheet-for-midterm ↔ threat-modeling-as-code-pytm-threatspec -> knowledge/connections/subnet-math-cheat-sheet-for-midterm--threat-modeling-as-code-pytm-threatspec.md
