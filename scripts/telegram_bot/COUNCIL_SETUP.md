# Council Telegram bridge — one-time setup

The council is a **separate Telegram bot** that fronts a group chat with you,
Yor, Loid and Luna. Your existing 1:1 bots for Yor, Loid and Luna are untouched;
keep talking to them privately as before. The council is only for the shared
room.

Everything here is something only you can do (create a bot, hold its token, make
the group). The code is already in place:

- `scripts/telegram_bot/council_bridge.py` — the orchestrator
- `scripts/launchd/com.andyherman.neural-bridge.council-telegram.plist` — the daemon
- `scripts/telegram_bot/council_notes.py` — the daily meeting notes and their nightly summary
- `scripts/launchd/com.andyherman.neural-bridge.council-notes.plist` — the nightly summary job

## 1. Create the council bot

In Telegram, message **@BotFather**:

```
/newbot
```

Give it a name (shown in the room, e.g. "Council") and a username ending in
`bot`. BotFather replies with an HTTP API token that looks like
`123456789:AA...`. Keep that message; you paste the token in step 3.

## 2. Turn OFF group privacy (required)

By default a bot in a group only sees messages that @-mention it or reply to it.
The council must see everything you say, so disable privacy:

```
/setprivacy
```

Pick the council bot, then choose **Disable**. This is the step that makes the
room work; without it the advisors only hear you when you @-mention the bot.

## 3. Store the token in the keychain

In Terminal, replace the placeholder with the token from step 1 and run:

```
security add-generic-password -s neural-bridge-telegram-council -a andyherman -w '<PASTE_TOKEN_HERE>'
```

The token never goes in a file or a config; the daemon reads it from the
keychain at startup, same as the other bridges.

## 4. Make the room

Create a new Telegram **group**. Add only two members: yourself and the council
bot. Do not add the Yor, Loid or Luna 1:1 bots; the council speaks for all three.

## 5. Start the daemon

```
cp scripts/launchd/com.andyherman.neural-bridge.council-telegram.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.andyherman.neural-bridge.council-telegram.plist
```

Watch it come up:

```
tail -f ~/Library/Logs/neural-bridge/council-telegram.log
```

You should see `Council Telegram bridge starting`. Send a message in the group
and watch the log route it.

## 6. First test

- "Help me think through the opening of a LinkedIn piece." Yor should answer; Loid should stay quiet.
- "I have a promo conversation with my manager next week." Loid should answer.
- "I want to write about what my last interview taught me." Both may answer, Loid on the career angle, Yor on the writing, each building on the other.
- "What's on my calendar tomorrow?" Luna should answer, from her calendar CLI.
- `@loid what do you think?` forces Loid; `@yor` forces Yor; `@luna` forces Luna.
  Telegram's @-tag of an advisor bot's username (e.g. `@Yor_agent_bot`) works
  the same way. "Luna Master" (the vault) and words like "lunar" do not.
- `/discuss 4 <question>` has Yor and Loid answer each other for up to 4 rounds
  (default 3, max 6) without you prompting each turn; Yor closes with three
  bullets, then Luna posts the next steps (owner, when, where each fits on your
  calendar, any conflict; nothing at all if there are none), and a status line
  says why it ended. `/stop`, or any new message, ends it after the turn in
  flight; a new message is then answered as usual.

## Notes and current limits (v1)

- **Voice works.** Send a Telegram voice note; it is transcribed locally by
  Whisper (same as Loid's bridge) and the room hears the text.
- **Who speaks** is decided by a small router each turn (or by an explicit
  `@yor` / `@loid` / `@luna`). Yor's and Loid's in-room manners (stay in lane,
  keep it short, defer to each other, silence is allowed) live in their SOUL
  under "In the shared room." Luna gets a short room framing from the bridge
  with each turn.
- **Luna is the executive-assistant lane**, not a third debater: time,
  calendar, inbox, commitments, reminders, scheduling, logistics, follow-ups.
  She runs exactly as in her 1:1 bridge (same charter, model and tools: her
  read-only calendar and inbox CLIs and her board CLI through Bash, which
  `hooks/guard_bash.py` limits to those commands). She cannot move a meeting or
  send mail from here either. Each council turn is stateless: the room's
  transcript is her context, not her DM history.
- **Loid runs on Opus 4.8 here** (passed explicitly). Note his 1:1 bot still
  runs on the NB default model; see the model-wiring note.
- **Memory:** your messages in the room build the shared `andyherman` card, so
  the room does develop both advisors' picture of you. Fine-grained
  who-said-what attribution is bounded but not yet perfect; that is the next
  thing to tighten.
- **Room log.** Everything said in the room is appended to a local JSONL file
  so Yor's nightly memory sees the whole room, including turns she did not
  speak in. See below.
- **Meeting notes.** The room log also becomes one note per day in the vault,
  kept current as the room talks, with a short summary added at night. See
  "Meeting notes" below.
- **To stop the daemon:** `launchctl unload ~/Library/LaunchAgents/com.andyherman.neural-bridge.council-telegram.plist`

## Room log

Path: `$COUNCIL_ROOM_LOG`, default
`~/Library/Application Support/neural-bridge/council-room.jsonl`. The bridge
creates the parent folder if it is missing and keeps the file at mode `0600`.
It is append-only and local; nothing reads it back into the room.

One JSON object per line, one line per message actually posted in the room:

```json
{"ts": 1767312000.25, "chat_id": -1001234567890, "speaker": "Luna", "text": "Next steps\n- ...", "mode": "discuss", "discussion": "1767311700.125000", "part": "next_steps"}
```

| Field | Meaning |
|---|---|
| `ts` | Unix time (float seconds) when the line was written |
| `chat_id` | Telegram chat id of the room (an integer; negative for a group) |
| `speaker` | `Andy`, `Yor`, `Loid` or `Luna` |
| `text` | The message as posted: Andy's text, the transcript of a voice note, or an advisor's whole reply (not split into Telegram parts) |
| `mode` | `message` for normal turns, `discuss` for a `/discuss` question, its turns, Yor's summary and Luna's next steps |
| `discussion` | The `/discuss` this line belongs to: one id per discussion (its start time as a string), shared by every line of it. `null` for a normal exchange |
| `part` | What the line was: `message` (a normal exchange), `question` (Andy's `/discuss` question), `turn` (a Yor or Loid turn in the back-and-forth), `summary` (Yor's closing summary), `next_steps` (Luna's closing post) |

`discussion` and `part` were added after the log started; lines written before
them simply lack the two keys. Readers that only know the first five fields
(the nightly exporter on the Hermes side reads `speaker`, `ts`, `text` and
`chat_id`) can ignore them.

What is not logged: `[PASS]` and `[DONE]` markers (and the turns they stand
for), error notices, the "heard:" echo of a voice note, status lines such as
"Discussion ended", and commands like `/stop`. No tokens or credentials are
ever written. A write that fails is noted in `council-telegram.log` and the
turn carries on.

## Meeting notes

One Markdown note per day in the Obsidian vault, built from the room log alone.

- **Folder:** `$COUNCIL_NOTES_DIR`, default `~/Documents/Luna Master/Council`
  (created if missing). The note for a day is `YYYY-MM-DD.md`, by local date.
  A `/discuss` that runs past midnight stays whole in the note of the day it
  started.
- **Live:** after every normal exchange (even one no advisor answered) and
  after every `/discuss`, the bridge rebuilds that day's note from the log.
  The rebuild is idempotent and writes atomically (temp file, then rename;
  mode `0600`), and an unchanged note is not rewritten. A failed write is
  logged as `NOTES render failed` in `council-telegram.log` and the room
  carries on.
- **Rebuilt, not edited:** anything edited by hand outside the summary block
  is replaced on the next rebuild. The summary block is kept byte for byte, so
  it can be edited.
- **Layout:** frontmatter (`type: council`, `date`, `participants` in the order
  Andy, Yor, Loid, Luna, `discussions` count, `tags: [council]`), then
  `# Council, YYYY-MM-DD`, a `## Summary` block, one
  `## HH:MM Discussion: <start of the question>` section per `/discuss` with
  `### Where they landed` (Yor's summary), `### Next steps` (Luna's, when she
  posted some) and the question and turns in a folded `Transcript` callout,
  then `## Other exchanges`: normal messages in folded callouts, one per
  conversation (a new one after three quiet hours). Lines written before the
  `discussion` and `part` fields existed show up under Other exchanges.
- **No em-dashes:** any in the room's text are written as a comma (or a colon
  at the end of a line). HTML comment delimiters in room text are written as
  entities so they cannot hide part of the note.
- A lock file beside the room log (`council-room.jsonl.notes.lock`) keeps the
  bridge and the nightly job from writing a note at the same time.

### Nightly summary

`com.andyherman.neural-bridge.council-notes` runs every day at 23:30 local:

```
.venv/bin/python -m scripts.telegram_bot.council_notes summarize
```

With no `--date` it covers yesterday (so lines after last night's run are
caught) and, after 22:00, today. For a day with council activity whose summary
is missing, or older than the day's last line, it asks `claude-haiku-5-5`
(effort low, no tools, through `claude_invoke.call_claude_sync`, so on the
fleet's Anthropic API route) for 3 to 6 bullets: decisions made, open
questions, next steps with an owner, from the transcript only. The bullets go
between the `<!-- council-summary:start last_ts=... -->` and
`<!-- council-summary:end -->` markers; `last_ts` is the newest room line the
summary covers. A day with no council lines makes no model call.

`install.sh` installs the job with the others. By hand:

```
.venv/bin/python -m scripts.telegram_bot.council_notes summarize --date 2026-11-02 --dry-run
```

`--dry-run` calls the model and prints the bullets without writing anything.
To build or rebuild a day's note from the log (for example a day from before
the notes existed):

```
.venv/bin/python -m scripts.telegram_bot.council_notes render --date 2026-11-01
```

Logs: `~/Library/Logs/neural-bridge/council-notes.stdout.log` and
`council-notes.stderr.log`. `COUNCIL_SUMMARY_MODEL` and
`COUNCIL_SUMMARY_TIMEOUT` (seconds, default 180) override the model and its
timeout; like the other settings they belong in `~/.hermes/.env`.
