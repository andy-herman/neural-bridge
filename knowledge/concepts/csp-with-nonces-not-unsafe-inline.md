---
slug: csp-with-nonces-not-unsafe-inline
verdict: PROMOTE
reason: factually accurate, reusable web-security concept consistent with INFO 310A (cybersecurity) corpus context; no adversarial signal, no overreach, slug well-formed
checks_triggered: []
compiled_at: 2026-05-10T15:22:35Z
compiler_version: "1.2"
sources:
  - agent: teaching-prep
    session_id: 31c340be-065d-b8bb-87b6-e0aa430d890e
    transcript_sha256: 31c340be065db8bb87b6e0aa430d890e9b4b45de0b64accc0cda325651514017
    source_log: daily-logs/teaching-prep/2026-05-10.md
    session_n: 1
---

# csp-with-nonces-not-unsafe-inline

Content Security Policy (CSP) configured with per-request nonces rather than the `unsafe-inline` source expression — the pattern that prevents arbitrary inline script execution while preserving the ability to run intentional inline scripts.

## Why this matters

`unsafe-inline` in a CSP `script-src` directive negates most of the policy's XSS protection: any injected inline script runs alongside intentional ones. Nonces thread the needle by generating a cryptographically random token per response and gating inline script execution on its presence. An injected script has no access to the nonce; an intentional one does.

This concept surfaced from INFO 310A lecture and lab corpus during a Stage 2/3 calibration pass, which suggests learners encounter `unsafe-inline` early (often copied from documentation examples) and need an explicit correction toward the nonce pattern before it becomes habitual.

## Key points

- `unsafe-inline` disables inline script blocking, which is the primary XSS mitigation CSP provides
- A nonce is a server-generated random value, unique per response, embedded in both the HTTP header and the trusted `<script>` tag
- CSP Level 2 (broadly supported) introduced nonces; no polyfill is needed for modern deployments
- Nonces are incompatible with caching the HTML response — each nonce must be generated fresh per request, which is an intentional trade-off, not a bug

## Related concepts

`[[content-security-policy]]` · `[[xss-prevention]]` · `[[http-security-headers]]`
