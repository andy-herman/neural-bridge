# Moonbase finite work bridge, protocol 1

**Source-only, opt-in, one manually submitted job per process.** This is not a
bot, scheduler, conversation relay, or telemetry producer. It does not load
private memories, change the released agent charters, or activate any service.
The [model-subprocess telemetry contract](MOONBASE_TELEMETRY.md) is unchanged.
Moonbase must remain usable when this optional adapter is absent.

## Fixed entry points and host boundary

Run from an operator-selected, trusted Neural Bridge checkout, using a fixed
operator-selected Python interpreter (Python 3.12+, POSIX):

```text
python -m scripts.moonbase_bridge catalog
python -m scripts.moonbase_bridge run
```

There are no user-supplied command, executable, model, effort, tool, timeout,
environment, output-file, or extra-directory arguments. The Moonbase server
authenticates and authorizes each submission, resolves a registered floor to a
canonical project root, caps concurrency at **one**, and persists receipts.
The browser must never choose the interpreter, checkout, environment, or
`projectRoot`. This local stdio boundary does not authenticate users itself.
It reads no global job store and never resumes or replays a job.

`catalog` prints exactly one JSON line and exits 0 (or exits 3 on unavailable
registry/platform, without partial data). Its shape is:

```text
{
  protocolVersion: 1,
  type: "catalog",
  agents: [{
    agentId: string, displayName: string,
    pluginDefined: boolean, discordRegistered: boolean
  }],
  profiles: [{
    profileId: "project-check" | "public-research",
    agentId: "automation-engineer" | "research",
    executionKind: "script" | "model",
    available: boolean, unavailableReason: string | null,
    timeoutSeconds: integer
  }],
  limits: {
    requestBytes: 16384, eventBytes: 16384, totalEventBytes: 262144,
    eventCount: 128, resultBytes: 32768
  }
}
```

The registry is the released plugin/Discord registry, not a second roster.
The catalog including its LF is at most **65536 bytes**, with at most **128
agents**, unique `agentId` strings of 1-64 ASCII characters matching
`[a-z][a-z0-9]*(?:-[a-z0-9]+)*`, and `displayName` strings of 1-80 ASCII characters
matching `[A-Za-z0-9 ._'-]+`. It has exactly the two profile entries below.
An oversized or invalid registry fails; it is never truncated.
Only the following fixed pairs are implemented; finding another agent in the
registry does not grant that agent a profile:

| Profile | Agent | Kind | Wall clock | Scope |
|---|---|---|---|---|
| `project-check` | `automation-engineer` | `script` | 30 seconds | Built-in filesystem/metadata checks; no LLM or project commands |
| `public-research` | `research` | `model` | 480 seconds | One isolated public-brief turn; at most 6 model turns; no retry |

Both agents must be plugin-defined and Discord-registered for their profile to
be available. The catalog returns safe reason codes, never credential, path,
CLI-error, or configuration contents.

`unavailableReason` is null for available profiles; otherwise it is exactly one
of `unsupportedPlatform`, `registryUnavailable`, `agentUnavailable`,
`researchDisabled`, `claudeMissing`, `cliPolicyUnverified`, `directAuthUnsupported`.
Consumers must treat unknown reasons as generic protocol failure, never display
unrecognized strings as error details.

Research is off unless the operator sets `NB_MOONBASE_PUBLIC_RESEARCH=1` in the
adapter's environment. **V1 still returns `cliPolicyUnverified` after that
opt-in:** offline help alone cannot certify isolation from managed-policy hooks.
The restricted caller is exercised only with mocks, not enabled by opt-in or a
CLI upgrade. A later reviewed implementation must establish that policy boundary
without bypassing organizational controls before offering availability. Merely
asking for the catalog never invokes a model. No installation, live setting,
or paid acceptance call is part of this change.

## Input

`run` accepts one UTF-8 JSON line, including its LF at most **16 KiB**. No BOM,
duplicate keys, non-finite numbers, unknown keys, coercions, or partial lines.
`protocolVersion` is the integer 1, not a string or boolean. UUIDs use canonical
lowercase hyphenated form.

Project check, exactly these keys:

```json
{"protocolVersion":1,"jobId":"11111111-1111-4111-8111-111111111111","profileId":"project-check","agentId":"automation-engineer","projectRoot":"/srv/projects/example"}
```

Public research, exactly these keys:

```json
{"protocolVersion":1,"jobId":"22222222-2222-4222-8222-222222222222","profileId":"public-research","agentId":"research","brief":"Compare public documentation for two open standards and cite primary sources."}
```

`brief` is 1-4000 characters, nonblank public text only. It is never combined
with project files or private history. Research rejects `projectRoot`; checks
reject `brief`. The server supplies the root, and the adapter independently
requires a bounded canonical absolute directory, refuses home, filesystem root,
broad system roots, and symlinked components, and opens descendants without
following symlinks. Root opening/validation happens inside the timed worker,
before `started`, so slow filesystem operations do not run outside supervision.
A registered floor is an authorization decision belonging
to the server; filesystem validation is not a substitute for it.

Keep stdin **open until the terminal event**. The only later permitted input is:

```json
{"protocolVersion":1,"type":"cancel","jobId":"11111111-1111-4111-8111-111111111111"}
```

One matching cancellation is sufficient; identical repeats are harmless.
Any other subsequent input interrupts the owned job and fails the invocation.
Closing stdin before terminal is owner disconnect: stop/reap the owned job and
report `interrupted`, not success. SIGINT/SIGTERM request cancellation; a
request to cancel alone is never proof that a process stopped.

## Output

Stdout is UTF-8 NDJSON, one complete object plus LF per frame:

```text
{
  protocolVersion: 1,
  jobId: UUID | null,
  sequence: integer,
  type: "started" | "progress" | "result" | "terminal",
  payload: object
}
```

Sequence starts at 1 and increases by exactly 1. A parsed valid request binds
every event to its job UUID. Invalid requests use `jobId: null` when no valid
UUID can be recovered. No timestamps, private paths, raw child stdout/stderr,
prompts, tool arguments, tool traces, environment values, or transcripts.
Consumers must escape all text and never interpret result text as HTML,
commands, or trusted instructions.

| Type | Exact payload |
|---|---|
| `started` | `{profileId, agentId, executionKind}` with the fixed catalog values |
| `progress` | `{stage:"scan", unit:"entries", completed:integer}`; actual visited entry count only |
| `result` | `{format:"plainText", chunkIndex:integer, text:string}` |
| `terminal` | `{outcome, reasonCode:string, exitCode:integer, cleanupConfirmed:boolean}` |

For accepted work: `started`, zero or more `progress`, zero or more `result`,
then exactly one `terminal`. A pre-start rejection has only `terminal`.
`started` means the real check/research subprocess has started, not that a job
was queued. Research has no detailed tool-progress events; show "research turn
active; detailed tool progress unavailable", not invented steps.

Result chunks are indexed from 0 without gaps, each at most **2048 UTF-8 text
bytes**, with at most **32 KiB** of text across the invocation. They are a
single report in order, provisional until `succeeded`. No partial report is
published as success. Every serialized frame including LF is at most 16 KiB;
the whole stream is at most 256 KiB and 128 events, including terminal.
The adapter reserves space for terminal. Limits cause a bounded failure,
never silent truncation or unbounded buffering.

The complete `reasonCode` vocabulary and its exit mapping:

| Exit | Allowed reason codes |
|---|---|
| 0 | `completed` |
| 2 | `invalidRequest`, `invalidControl`, `invalidRoot` |
| 3 | `profileUnavailable`, `registryUnavailable`, `unsupportedPlatform` |
| 4 | `checkFailed`, `checkLimit`, `researchFailed`, `resultRejected`, `outputLimit`, `workerProtocol` |
| 5 | `timedOut` |
| 6 | `cancelled` |
| 7 | `inputClosed`, `processInterrupted`, `cleanupUnknown`, `internalError` |

These are fixed codes, not raw exception strings. Unknown codes are protocol
failure. Initial input must arrive within 5 seconds. Cleanup gets up to 2 seconds
after SIGTERM and 2 after SIGKILL; a blocked output write gets at most 1 second.
The job wall clock excludes that bounded cleanup/terminal-delivery allowance.

| Exit | Terminal outcome | Meaning |
|---|---|---|
| 0 | `succeeded` | Built-in checks finished, or one research turn returned a bounded report |
| 2 | `failed` | Invalid request/control input |
| 3 | `failed` | Profile, registry, platform, or CLI policy unavailable |
| 4 | `failed` | Check/research/output failure |
| 5 | `timed_out` | Fixed wall-clock limit; owned process cleanup confirmed |
| 6 | `cancelled` | Cancellation handled and owned process cleanup confirmed |
| 7 | `interrupted` | Disconnect, unexpected process loss, or cleanup not provable |

`cleanupConfirmed` must be true for succeeded/timed_out/cancelled. If descendant
death cannot be established, terminal is `interrupted` with
`reasonCode:"cleanupUnknown"` and `cleanupConfirmed:false`, even after a cancel
request. Only the invocation's owned process group is signalled; no global
process-name kills. Direct children are waited/reaped. Output-pipe failure or
uncatchable process death can prevent terminal delivery.

**Consumer rule:** stdout EOF without exactly one valid terminal, sequence/bounds
violation, or disagreement between process exit and terminal is `interrupted`,
even if the OS exit code is 0. After timeout, crash, disconnect, or server
restart, never replay automatically. Persist the interrupted receipt and require
a fresh explicit submission.

Success is not a claim of verified research truth, delivered chat, passing the
project's own test suite, or model activity for scripts. Display SCRIPT and MODEL
distinctly. No event from this channel is written into the v1 telemetry file.

## Built-in check boundaries

The fixed Python scanner uses descriptor-relative no-follow filesystem reads.
It never runs package scripts, package managers, repository executables, shell
commands, Git, hooks, configuration code, or imported project modules. It
counts selected source/document types and validates only bounded root metadata
(`package.json` and `pyproject.toml`) as data. Reports contain fixed check names,
counts and finding codes, never file contents or paths.

Hard ceilings: 4096 visited directory entries, depth 12, 2048 selected regular
files, 32 MiB of selected file sizes, and 64 KiB per metadata file. Hidden
entries (including `.git` and `.env`), symlinks, special/hard-linked files,
dependency/build directories, and unsupported file types are skipped.
A ceiling or unreadable required metadata fails explicitly. Missing README or
metadata is a finding, not a claim that a project test suite failed.

## Research policy and known limits

Use the released Research charter and `model_for`/`effort_for` helpers, but not
`run_agent_turn`'s mention prompt, session store, retries, add-dirs, private grants,
Honcho, voice, or delivery. The separate caller uses the existing low-level
provider-environment and empty-MCP helpers; normal Discord/Telegram invocation
defaults remain unchanged.

The required CLI policy restricts **available** tools using
`--tools WebSearch,WebFetch`, not merely `--allowedTools`; no local file,
shell, MCP, delegation or code-execution tool is present in that requested tool
set. The caller requests bare/safe/restricted mode, disabled skills and
persistence, an isolated empty working directory and throwaway identity, and
max turns and wall clock. **This does not certify that every managed-policy
hook is disabled.** Managed policy remains authoritative; the profile is
unavailable rather than bypassing it. No permission bypass, broader tools,
direct-auth fallback, model fallback, resume, or automatic retry is allowed. The operator's current proxy
routing remains the source of truth; direct login is unavailable when its
authentication would conflict with isolation.

Offline help inspected on 2026-10-02 advertises `--bare`, `--safe-mode`,
`--restricted`, `--tools`, `--disable-slash-commands`,
`--no-session-persistence`, `--setting-sources`, `--strict-mcp-config`,
`--mcp-config`, `--system-prompt`, `--session-id`, and `--effort`, but **not
`--max-turns`**. Help intentionally omits some supported flags, so this means
**unverified**, not necessarily unsupported. Even an all-flags help listing
would not resolve the managed-policy isolation question. These findings are not
an invitation to remove the gate or inspect private settings without approval.
Tests use synthetic help and mocked model output; they are not live model QA.

## Runtime packaging

Entry point: `scripts/moonbase_bridge.py`; its fixed, isolated worker re-enters
that same file with Python `-I -B -u` and the private `_worker` selector. The
worker receives its validated request over a pipe, then opens/pins its root.
Scanner: `scripts/moonbase_project_check.py`. No third-party package is required
by catalog or checks. Preserve the trusted checkout's `scripts/` and `hooks/`
package layout rather than copying one loose entry file.

The adapter imports only the registry discovery function from
`scripts/discord_bot/agent_telemetry.py`, never its writer/Invocation APIs.
Discovery reads `scripts/discord_bot/agents.json` and filenames in
`plugins/neural-bridge-core/agents/*.md`; it does not resolve bot tokens.

The optional research path additionally uses
`scripts/discord_bot/claude_invoke.py` and its `hooks/claude_env.py` routing/MCP
helpers, plus `scripts/discord_bot/mention.py` for the released charter/model/
effort lookup. The latter imports `honcho_client.py`, `memory_telemetry.py`,
`progress_log.py`, and `hooks/wiki_recall.py`; those imports are stdlib-only
and this caller does not invoke their memory, network or write APIs. Preserve
`plugins/neural-bridge-core/agents/research.md`. No Discord/Telegram daemon,
`agent_runtime`/session-store import, env-file loader, YAML loader, private grants,
credential lookup, provider ping, or installation is needed for catalog/checks.
Only an explicitly authorized, available research job selects the released
provider route. Its installed Claude executable must advertise every required
flag above (also `--allowedTools`, `--no-chrome`, `--output-format`, `--model`);
no CLI version string alone certifies those capabilities.

Only a successfully parsed final result report may become result events.
Raw stream messages and error snippets are never forwarded. Oversize, malformed,
unsafe-path/credential-bearing, or prompt-echoing reports are refused with a
fixed reason code, not emitted or silently rewritten.

## Consumer fixtures

[`examples/moonbase-work-v1.json`](examples/moonbase-work-v1.json) contains
synthetic catalog, request, cancel, successful script/research streams, rejected
request, cancellation, timeout, cleanup-unknown, and EOF-without-terminal
examples. Fixtures are contract data only, never live fallback observations.
