# Council Telegram bridge — one-time setup

The council is a **separate Telegram bot** that fronts a group chat with you,
Yor, Loid and Luna. Your existing 1:1 bots for Yor, Loid and Luna are untouched;
keep talking to them privately as before. The council is only for the shared
room.

Everything here is something only you can do (create a bot, hold its token, make
the group). The code is already in place:

- `scripts/telegram_bot/council_bridge.py` — the orchestrator
- `scripts/launchd/com.andyherman.neural-bridge.council-telegram.plist` — the daemon

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
- **To stop the daemon:** `launchctl unload ~/Library/LaunchAgents/com.andyherman.neural-bridge.council-telegram.plist`

## Room log

Path: `$COUNCIL_ROOM_LOG`, default
`~/Library/Application Support/neural-bridge/council-room.jsonl`. The bridge
creates the parent folder if it is missing and keeps the file at mode `0600`.
It is append-only and local; nothing reads it back into the room.

One JSON object per line, one line per message actually posted in the room:

```json
{"ts": 1767312000.25, "chat_id": -1001234567890, "speaker": "Luna", "text": "Next steps\n- ...", "mode": "discuss"}
```

| Field | Meaning |
|---|---|
| `ts` | Unix time (float seconds) when the line was written |
| `chat_id` | Telegram chat id of the room (an integer; negative for a group) |
| `speaker` | `Andy`, `Yor`, `Loid` or `Luna` |
| `text` | The message as posted: Andy's text, the transcript of a voice note, or an advisor's whole reply (not split into Telegram parts) |
| `mode` | `message` for normal turns, `discuss` for a `/discuss` question, its turns, Yor's summary and Luna's next steps |

What is not logged: `[PASS]` and `[DONE]` markers (and the turns they stand
for), error notices, the "heard:" echo of a voice note, status lines such as
"Discussion ended", and commands like `/stop`. No tokens or credentials are
ever written. A write that fails is noted in `council-telegram.log` and the
turn carries on.
