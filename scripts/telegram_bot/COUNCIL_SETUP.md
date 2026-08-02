# Council Telegram bridge — one-time setup

The council is a **third, separate Telegram bot** that fronts a group chat with
you, Yor, and Loid. Your existing 1:1 bots for Yor and Loid are untouched; keep
talking to them privately as before. The council is only for the shared room.

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
bot. Do not add the Yor or Loid 1:1 bots; the council speaks for both.

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
- `@loid what do you think?` forces Loid; `@yor` forces Yor.

## Notes and current limits (v1)

- **Voice works.** Send a Telegram voice note; it is transcribed locally by
  Whisper (same as Loid's bridge) and the room hears the text.
- **Who speaks** is decided by a small router each turn (or by an explicit
  `@yor` / `@loid`). Their in-room manners (stay in lane, keep it short, defer to
  each other, silence is allowed) live in each advisor's SOUL under
  "In the shared room."
- **Loid runs on Opus 4.8 here** (passed explicitly). Note his 1:1 bot still
  runs on the NB default model; see the model-wiring note.
- **Memory:** your messages in the room build the shared `andyherman` card, so
  the room does develop both advisors' picture of you. Fine-grained
  who-said-what attribution is bounded but not yet perfect; that is the next
  thing to tighten.
- **To stop the daemon:** `launchctl unload ~/Library/LaunchAgents/com.andyherman.neural-bridge.council-telegram.plist`
