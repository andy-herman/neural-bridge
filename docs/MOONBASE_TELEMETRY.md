# Private specialist telemetry for Moonbase

**Opt-in; not enabled by installing this code.** This is a local, bounded JSON
snapshot, not a network server. It observes **model-subprocess execution**, not
task completion, tool success, or successful delivery of a reply.

The registry comes from the union of
`plugins/neural-bridge-core/agents/*.md` filenames and Discord config IDs.
Currently there are 14 plugin definitions and 13 Discord registrations; Loid
is defined but not Discord-registered, and runs through his Telegram and
Council bridges. A definition is not a live process. Discord's existing token
resolution can skip a configured identity; telemetry does not read credentials.

Fleet is an application-level heartbeat. Its contents, including an empty
aggregate, do not prove that specialists are absent, idle, or offline.

## Configuration and safe activation

| Setting | Meaning |
|---|---|
| `NB_AGENT_TELEMETRY_PATH` | Producer opt-in: an absolute JSON filename outside all Git checkouts. An unset/empty effective value means no telemetry files or heartbeat thread. |
| `MOONBASE_NEURAL_BRIDGE_SNAPSHOT` | Moonbase server's explicit local source filename, pointing to the same snapshot. Never expose it to browsers or accept a request-supplied filename. |

A suggested, **not automatic**, destination is
`$HOME/.local/state/neural-bridge/moonbase/agent-telemetry.json`.
The dedicated containing directory must be owner-controlled mode 0700;
snapshots, leases, and merge locks must be owner-controlled regular files
mode 0600. Unsafe existing modes, owners, symlinked destination directories or
leaves, hard-linked files, and paths inside checkouts are refused.
New private directories can be created; existing directories, home, and
unrelated ancestors are never chmodded. Do not place the file in a synced vault.

Populate only the real registry, without starting a bot:

```sh
.venv/bin/python -m scripts.discord_bot.agent_telemetry export-registry \
  --path "$HOME/.local/state/neural-bridge/moonbase/agent-telemetry.json"
```

On an absent file this exports `writers: []`: every identity is **unobserved**.
On an existing valid file it preserves every writer slot, heartbeat, counter,
and event. An error returns nonzero and does not overwrite the file.
The exporter does not use models, the network, credential stores, or transcripts.
This is the registry fallback, not a hardcoded roster or synthetic activity.

Live observation requires deployed code and `NB_AGENT_TELEMETRY_PATH` in each
desired process, inherited or loaded at startup as described below, followed
by a separately approved normal restart. Setting a variable in a shell does not
change an existing launchd process. This integration does not edit installed
plists or restart anything. **The existing main-branch auto-reloader can restart Discord and
loaded Telegram bridges after merge. Coordinate deployment before merging.**
All selected producers should use the same deployed registry revision.

For a separately authorized manual run, export the variable before invoking
the existing daemon command. Do not launch a duplicate production daemon as a
telemetry test. A duplicate telemetry source cannot steal the existing lease,
but this does not replace the bots' own process-management rules.

### Persistent opt-in (source-only follow-up)

Discord now calls the existing `scripts.env_file.load_default_env` before
observer setup with only `keys={agent_telemetry.ENV_PATH}`. No other file
settings are imported or overridden in Discord: credentials, provider/routing
flags, and `NB_AGENT_ID` remain untouched. The three Telegram bridge entrypoints
keep their existing unfiltered `load_default_env()` calls.

A telemetry value rejected by the process environment produces only the fixed
`invalid_env_value` diagnostic. Discord still starts with telemetry off; the
input value is never logged. The shared loader's error behavior is unchanged.

The default precedence is the inherited process environment (including an
explicitly empty string), then `~/.hermes/.env`, then the optional ignored
repository `.env`. The earlier source wins. A local file supplies the path only
when neither the process nor the shared file defines it. Unsetting an inherited
value exposes the file defaults; an inherited empty string explicitly opts out
even if a file contains a path.

For a separately approved persistent configuration, the operator can create the
ignored repository `.env` with exactly this one credential-free setting,
replacing the placeholder with a literal absolute filename:

```dotenv
NB_AGENT_TELEMETRY_PATH=/absolute/private/path/agent-telemetry.json
```

The parser does not interpolate `$HOME` or expand `~` in values. The previously
demonstrated live pilot used a transient launchd environment; this source-only
follow-up does not establish that file-based persistence is deployed. It creates
no real env file and changes no service, plist, or launchctl environment.
Deployment still requires the operator's review and separately approved
pause-first rollout.

### Operator runbook: separately approved live pilot

These are instructions, not actions performed by this change. The existing
deployment is a macOS GUI-user launchd domain; the tracked plists wrap the
Python entrypoints in `caffeinate`. All four processes must reload code and
inherit the opt-in variable to obtain all four observation slots.

| Source | Existing service label | Entrypoint |
|---|---|---|
| `discord` | `com.andyherman.neural-bridge.discord-bot` | `scripts.discord_bot.main` |
| `telegram-luna` | `com.andyherman.neural-bridge.luna-telegram` | `scripts.telegram_bot.luna_bridge` |
| `telegram-loid` | `com.andyherman.neural-bridge.loid-telegram` | `scripts.telegram_bot.loid_bridge` |
| `telegram-council` | `com.andyherman.neural-bridge.council-telegram` | `scripts.telegram_bot.council_bridge` |

The existing `com.andyherman.neural-bridge.auto-reload` job polls main and calls
the broad `scripts/launchd/install.sh`, then restarts loaded Telegram bridges.
That installer manages other scheduled jobs too. Do not run it as a narrowly
scoped telemetry activation. Its documentation also records that Luna's
installed plist may be a symlink into the deployment checkout: do not blindly
edit it, replace it, or dirty tracked production configuration.

1. Obtain explicit permission for the merge/deployment, a temporary pause of
   auto-reload, one launchd user-session environment variable, and restarting
   **only the four listed services**. Name the private destination. Agree on
   interruption risk before doing anything operational.
2. Ask users to finish/stop submitting work and refresh non-sensitive service
   and child-process metadata immediately before rollout. There is no universal
   drain API. No visible Claude child is not proof that a handler, delivery,
   queue item, or detached task is finished. If idleness cannot be established,
   obtain explicit acceptance of interruption rather than declaring it drained.
3. Have the authorized deployment operator pause the auto-reloader before
   merging, then advance the clean deployment checkout through the existing
   fast-forward deployment mechanism. Do not merge first and race the watcher.
   Record the previous deployed revision for rollback.
4. For a minimal reversible pilot without editing plists or credential-bearing
   env files, the operator can use `launchctl setenv NB_AGENT_TELEMETRY_PATH
   /absolute/private/path/agent-telemetry.json` in the correct user domain.
   This affects subsequently launched jobs in that user session, not already
   running processes, and is **not a reboot-persistent configuration**.
   Verify only this named setting; do not dump entire process environments.
5. Restart each already-installed selected job with
   `launchctl kickstart -k "gui/$(id -u)/<service-label>"`, only after the drain
   decision. This can interrupt work; it is not a graceful drain command.
   Do not start second copies by hand. Restore the paused watcher through its
   existing installed plist only after the intended deployment is verified.
6. Check mode/ownership, all intended source slots, distinct instance UUIDs,
   independently advancing heartbeats, and the consumer's real status labels.
   Confirm Discord connectivity from its actual callbacks; Telegram null is
   still unmeasured. Never trigger paid model work as a smoke test; later
   authorized normal work can provide genuine activity evidence.

Rollback of the pilot is `launchctl unsetenv NB_AGENT_TELEMETRY_PATH` followed
by the same explicitly approved targeted restarts; running processes retain
their previous environment until restarted. Leave the private snapshot for
stopped/stale diagnosis instead of forging empty/live state. If code rollback
is needed, keep auto-reload paused while the operator follows the approved
revision/revert procedure; do not use destructive checkout resets. Re-enable
the normal watcher after the correct code is in place. A durable launch
configuration, or a different restart/drain policy, requires its own decision.

## Version 1 contract

The [synthetic example](examples/moonbase-telemetry-v1.json) illustrates the
shape with an abbreviated roster. **Never use it as a live-data fallback.**
Every field below is required; nullable values are explicit JSON `null`.
All timestamps are RFC 3339 UTC strings ending in `Z`.

```text
Snapshot {
  schema_version: 1,
  scope: "model-subprocess",
  generated_at: timestamp,
  registry_generated_at: timestamp,
  agents: RegistryAgent[],
  writers: Writer[]
}
RegistryAgent {
  id: slug,
  display_name: string,
  plugin_defined: boolean,
  discord_registered: boolean
}
Writer {
  source: "discord" | "telegram-luna" | "telegram-loid" | "telegram-council",
  instance_id: UUID,
  status: "running" | "stopped",
  started_at: timestamp,
  heartbeat_at: timestamp,
  heartbeat_interval_seconds: 10,
  stale_after_seconds: 45,
  agents: Observation[],
  events: Event[]
}
Observation {
  id: slug,
  enabled: boolean,
  connected: boolean | null,
  status: "starting" | "idle" | "working" | "error" |
          "disconnected" | "disabled" | "stopped",
  active_jobs: integer,
  last_activity_at: timestamp | null,
  last_outcome: "succeeded" | "failed" | "cancelled" | "interrupted" | null,
  last_outcome_at: timestamp | null,
  counters: {
    started: integer,
    succeeded: integer,
    failed: integer,
    cancelled: integer,
    interrupted: integer
  }
}
Event {
  sequence: positive integer,
  at: timestamp,
  agent_id: slug | null,
  type: "writer_started" | "writer_stopped" |
        "agent_enabled" | "agent_disabled" | "agent_ready" |
        "agent_disconnected" | "job_started" | "job_succeeded" |
        "job_failed" | "job_cancelled" | "job_interrupted" | "wait_cancelled",
  active_jobs: integer
}
```

The slug is the role. Display names come from validated configuration identity
labels, or deterministic humanization of the slug. No descriptions or plugin
body text are exported. A config-only identity has `plugin_defined: false`.
Registry order is deterministic. Identity IDs, per-writer agent IDs, and
source names are unique in their arrays.

Bounds: 512 KiB UTF-8 JSON; 128 registry entries; the four named source slots;
128 observations per source; 64 most recent events per source; 256 active jobs
per observation. Slugs match `^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$`, maximum 64
ASCII characters. Display names contain at most 80 ASCII letters, digits,
spaces, periods, underscores, apostrophes, or hyphens. Counters and sequences
are nonnegative JavaScript-safe integers; sequences start above zero.
Overflow refuses telemetry instead of inventing counts.

## Interpretation

| Field/state | What it establishes |
|---|---|
| Registry membership | An actual definition or configured identity, not observation of execution. |
| `enabled` | This process accepted the identity in existing runtime setup, or observed its real model invocation. A Discord credential-resolution skip initially records disabled. |
| `connected` | Last measured Discord gateway state. Telegram uses null: process initialization does not certify polling health. |
| `starting` | The existing startup callback has not completed. |
| `idle` | No observed model invocation in this source, not global inactivity. |
| `working` | One or more observed invocation spans, including overlapping work. Gateway disconnection does not end model work. |
| `error` | No active invocation, and the last execution outcome failed or lost observation. Later successful work supersedes it. |
| `disconnected` | A measured gateway disconnect with no active model work. Reconnection does not erase a prior failure. |
| `stopped` | The observer stopped, not proof that all child processes died. Never live work. |
| `interrupted` outcome | **Observation lost**, not successful completion and not proof the subprocess was killed. |

`last_activity_at` changes on execution start, terminal outcome, or observation
loss, never on writer heartbeats or gateway changes. `last_outcome_at` changes
only with a terminal execution outcome. Counters persist per source/specialist:

```text
started = succeeded + failed + cancelled + interrupted + active_jobs
```

They count invocation attempts, not user tasks. A failed resume followed by
the existing fresh-session retry counts as two attempts. Missing CLI and
timeout failures remain failed attempts. A reaped signal-terminated process
or an aborted execution boundary can be cancelled; observer code never kills
or retries a process.

Cancelling the async waiter emits `wait_cancelled` but cannot decrement a
still-running executor worker's active count, increment `cancelled`, or change
the last outcome. The worker records its eventual actual result. Cancellation
while queued, before the worker starts, invents no execution.

A new instance UUID carries forward validated counters and event sequence.
Predecessor unfinished counts become interrupted; no old active span is
resumed. Graceful shutdown similarly closes remaining observations as
interrupted before writing stopped. Abrupt death leaves last-known data until
stale or a replacement records observation loss. Delayed completion from an
already-closed observer cannot overwrite the replacement.

Events are a bounded UI hint, not a durable audit log. Sequences increase in
array order across restarts. `active_jobs` is the remaining count for the named
agent, or the source-wide total for a null agent. Establish a high-water mark
on initial load/reconnection; do not replay old events as new work. Current
state plus freshness, not a past event, authorizes live animation.

## Freshness and consumer errors

Poll every two seconds. A running writer is stale when its own heartbeat is
more than 45 seconds old. **Outer `generated_at`, file modification time, a
registry export, or another healthy writer cannot refresh that source.**
An explicitly stopped writer remains stopped, regardless of timestamp.

Reject timestamps more than five seconds in the future, checking every
envelope, registry, writer, observation, and event timestamp. Within that fixed
allowance, clamp negative age to zero. Never extend freshness indefinitely for
a future timestamp. `started_at` must not exceed its writer heartbeat;
observation/event times must not exceed it by more than five seconds. Historical
events and outcomes may predate the current instance start.

For each current registry identity, derive the consumer status:

1. Any fresh running source with positive active work: `working`.
2. Otherwise any stale running observation: `stale`, even with another fresh idle source.
3. Otherwise first applicable fresh state: `error`, `disconnected`, `starting`, `idle`, `disabled`.
4. Only stopped observations: `stopped`.
5. No observations: `unobserved`.

Always retain source-level stale/disconnected coverage badges, even while
another source is working. Current active count sums only fresh running
sources; stale active counts are last-known. Join observations against the
current registry; preserved removed-role observations cannot invent identities.

Missing, unreadable, malformed, oversized, unsupported-version, or future-invalid
input means a visible feed error, never zero specialists or sample activity.
An old valid registry may remain visible as last-known/unobserved with the
error shown, but cached work cannot stay labelled live. Explicitly project
approved fields; never forward unknown fields to browsers.

## Writer failure and concurrency

Each source owns a process-lifetime exclusive lease. A duplicate logs
`source_busy` and disables only its own telemetry. It never steals an incumbent
lease based on staleness or changes either bot's behavior. Lease/merge-lock
files remain on disk; an existing filename does not itself mean a held lease.
Do not delete or replace lock files while a producer is running.

A separate nonblocking lock covers read, strict validation, merging only the
owning slot, and atomic replacement using a same-directory private temporary.
Other active, stale, and stopped slots are preserved. A later registry export
is not overwritten on every heartbeat by an old startup catalog.
The writer retains the prior complete file on failure, logs fixed safe codes,
and retries updated in-memory state on a later heartbeat. A contended final
shutdown write may leave the prior snapshot to age out; it does not falsely
claim a persisted stopped state. Invalid snapshots are never silently reset.

Errors are optional-observer errors, not bot errors. Their log lines contain
fixed codes, not raw exception text, paths, or transport content. The export CLI
returns an error instead of a success-shaped fallback.

## Coverage and checks

Observed surfaces: Discord mentions and attributed PM summary/triage/squad
calls; Luna Telegram; Loid Telegram; Loid's Council invocations. Source identity
is assigned by the existing entrypoint. `telemetry_agent_id` is separate from
security `NB_AGENT_ID`; tools, approvals, budgets, environments, and retry
behavior are unchanged.

Not observed: standalone plugin sessions, scheduled Echo/content/check-in jobs,
nested agent internals, Council classifier/Yor, whole tasks/actions, delivery.
No inference about those surfaces follows from missing observations.

No prompts, responses, Discord/user/conversation/session IDs, secrets, token
usage, tool arguments, raw errors, home paths, or transcripts enter this file.
The UUID identifies the observer only. Nothing sends the snapshot externally.
Production emission is suppressed under test runners; tests instantiate
explicit temporary writers with fake lifecycles instead.

```sh
.venv/bin/python -m pytest scripts/test_env_file.py scripts/discord_bot/test_agent_telemetry.py -q
.venv/bin/python -m pytest hooks/ scripts/ -q
```

Tests cover actual cross-process leases/merges, concurrent readers, overlapping
jobs, cancelled waiters, stopped/restarted observers, safe registry projection,
future times, private paths, atomic failures, allowlisted env-file precedence,
and mocked transport entrypoints with temporary default env paths.
Passing fixtures does not establish that a production daemon is running.
