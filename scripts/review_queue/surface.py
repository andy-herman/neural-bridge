"""The review queue's surface: Luna's Telegram bot.

Luna's chat is the one channel Andy still reads, on his phone, and the bridge
already restricts it to LUNA_TELEGRAM_ALLOWED_USERS. This module adds, to
that same bot:

  - a pusher loop: every TICK_SECONDS it sends a card for each new item of a
    pushable kind, redraws cards whose item changed (decided, applied,
    cleared), wakes snoozed items, runs pending applies, and writes the
    heartbeat the health check and the failure watcher read. It also syncs
    the quarantine (captures) and the loop engineer's open PRs.
  - button callbacks: approve and reject (two taps where they publish or
    delete), acknowledge, answer, snooze a day, retry
  - answers: a reply to a question's card, or to the prompt its Answer button
    sends, is the answer. It is handled before Luna's chat handler sees the
    message, so an answer never turns into a conversation turn.
  - /queue: everything still waiting on Andy, with a button per item

Button presses are checked against the allowlist themselves; a press from
anyone else does nothing and is logged.

Which kinds are pushed is health.push_kinds(): captures from day one, alerts
since the shadow week ended (docs/REVIEW_QUEUE.md).
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Callable

from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from . import actions, appliers, captures, health, pr_reviews, publish, render
from . import store as st

TICK_SECONDS = 15
CAPTURE_SYNC_SECONDS = 600
LIST_LIMIT = 10
CAPTURE_SYNC_KEY = "capture_sync"
PR_SYNC_KEY = "pr_review_sync"
# Runs before Luna's own text handler (group 0), so a queue answer stops there.
ANSWER_HANDLER_GROUP = -1

_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def _markup(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup | None:
    if not rows:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data)
                                  for label, data in row] for row in rows])


def _not_modified(exc: Exception) -> bool:
    return isinstance(exc, BadRequest) and "not modified" in str(exc).lower()


def _message_gone(exc: Exception) -> bool:
    return isinstance(exc, BadRequest) and "not found" in str(exc).lower()


Applier = appliers.Applier


class QueueSurface:
    def __init__(self, *, store: st.Store, repo: Path, chat_ids: Callable[[], set[int]],
                 log: Callable[[str], None], push_kinds: tuple[str, ...] | None = None,
                 appliers: dict[str, Applier] | None = None,
                 capture_sync: Callable[[], object] | None = None,
                 pr_sync: Callable[[], object] | None = None):
        self.store = store
        self.repo = repo
        self.chat_ids = chat_ids
        self.log = log
        self.push_kinds = push_kinds if push_kinds is not None else health.push_kinds()
        self.appliers = appliers if appliers is not None else _default_appliers(repo)
        self._capture_sync = capture_sync or (lambda: captures.sync(store, repo))
        self._last_capture_sync = 0
        self._pr_sync = pr_sync or (lambda: pr_reviews.sync(store))
        self._last_pr_sync = 0
        self._apply_lock = asyncio.Lock()
        self._send_failures: dict[str, int] = {}
        self._task: asyncio.Task | None = None
        # asyncio keeps only weak references to tasks; hold them until done.
        self._background: set[asyncio.Task] = set()

    # ---------- wiring ----------

    def install(self, app: Application) -> None:
        app.add_handler(CallbackQueryHandler(self.on_callback, pattern=r"^q:"))
        app.add_handler(CommandHandler("queue", self.cmd_queue))
        app.add_handler(MessageHandler(filters.REPLY & filters.TEXT & ~filters.COMMAND, self.on_reply),
                        group=ANSWER_HANDLER_GROUP)

    async def start(self, app: Application) -> None:
        self.log(f"QUEUE surface starting: db={self.store.path} push={','.join(self.push_kinds) or 'none'}")
        self._task = asyncio.create_task(self._run(app.bot), name="review-queue-pusher")

    async def stop(self, app: Application) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self, bot) -> None:
        while True:
            try:
                await self.tick(bot)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # No heartbeat is written for a failed tick, so a loop that
                # keeps failing shows up in the health check, not just here.
                self.log(f"QUEUE tick failed: {type(exc).__name__}: {exc}")
            await asyncio.sleep(TICK_SECONDS)

    # ---------- the loop ----------

    async def tick(self, bot, now: int | None = None) -> None:
        now = now if now is not None else int(time.time())
        woke = self.store.wake_due(now)
        if woke:
            self.log(f"QUEUE woke {len(woke)} snoozed item(s)")
        stale = self.store.fail_stale_applying(now - health.APPLYING_MAX_AGE, now=now)
        if stale:
            self.log(f"QUEUE apply interrupted, marked failed: {stale}")
        expired = actions.expire_stale(self.store, now) + publish.expire_stale(self.store, now)
        if expired:
            self.log(f"QUEUE expired undecided: {expired}")

        if now - self._last_capture_sync >= CAPTURE_SYNC_SECONDS:
            self._last_capture_sync = now
            try:
                created, cleared = await asyncio.to_thread(self._capture_sync)
                self.store.meta_set(CAPTURE_SYNC_KEY, f"ok {now}")
                if created or cleared:
                    self.log(f"QUEUE captures: +{len(created)} -{len(cleared)}")
            except Exception as exc:
                self.store.meta_set(CAPTURE_SYNC_KEY, f"error {now} {type(exc).__name__}: {exc}"[:300])
                self.log(f"QUEUE capture sync failed: {type(exc).__name__}: {exc}")

        if now - self._last_pr_sync >= pr_reviews.SYNC_SECONDS:
            self._last_pr_sync = now
            try:
                created, cleared = await asyncio.to_thread(self._pr_sync)
                self.store.meta_set(PR_SYNC_KEY, f"ok {now}")
                if created or cleared:
                    self.log(f"QUEUE PR reviews: +{len(created)} -{len(cleared)}")
            except Exception as exc:
                self.store.meta_set(PR_SYNC_KEY, f"error {now} {type(exc).__name__}: {exc}"[:300])
                self.log(f"QUEUE PR review sync failed: {type(exc).__name__}: {exc}")

        for item in self.store.needing_render(self.push_kinds):
            for chat_id in sorted(self.chat_ids()):
                await self._send_card(bot, chat_id, item)

        for rendered, item in self.store.stale_renders():
            await self._redraw(bot, rendered.chat_id, rendered.message_id, item)

        await self.apply_pending(bot)
        health.write_status(self.store, self.push_kinds, now)

    async def _send_card(self, bot, chat_id: int, item: st.Item) -> None:
        try:
            msg = await bot.send_message(
                chat_id=chat_id, text=render.card_text(item), parse_mode=ParseMode.HTML,
                reply_markup=_markup(render.keyboard(item)), link_preview_options=_NO_PREVIEW)
        except TelegramError as exc:
            n = self._send_failures.get(item.id, 0) + 1
            self._send_failures[item.id] = n
            if n == 1 or n % 40 == 0:  # first failure, then every ~10 minutes
                self.log(f"QUEUE send failed for {item.id} (attempt {n}): {exc}")
                self.store.log_event(item.id, actor="telegram", event="render_failed",
                                     detail=str(exc)[:200])
            return
        self._send_failures.pop(item.id, None)
        self.store.add_render(item, chat_id, msg.message_id)
        self.log(f"QUEUE sent {item.kind} {item.id} to chat {chat_id}")

    async def _redraw(self, bot, chat_id: int, message_id: int, item: st.Item) -> None:
        rendered = st.Render(item.id, chat_id, message_id, 0, 0, None)
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=render.card_text(item),
                parse_mode=ParseMode.HTML, reply_markup=_markup(render.keyboard(item)),
                link_preview_options=_NO_PREVIEW)
        except TelegramError as exc:
            if _message_gone(exc):
                self.store.mark_shown(rendered, item.rev, close=True)
                return
            if not _not_modified(exc):
                self.log(f"QUEUE redraw failed for {item.id}: {exc}")
                return
        self.store.mark_shown(rendered, item.rev, close=render.is_closed(item))

    # ---------- applying decisions ----------

    async def apply_pending(self, bot=None) -> None:
        async with self._apply_lock:
            for item in self.store.items(states=(st.DECIDED,), include_probes=True):
                await self._apply_one(item)

    async def _apply_one(self, item: st.Item) -> None:
        applier = self.appliers.get(item.kind)
        if not self.store.claim(item.id, actor="applier"):
            return
        if applier is None:
            self.store.finish(item.id, False, f"no applier for {item.kind} items yet", actor="applier")
            return
        try:
            ok, result = await asyncio.to_thread(applier, item)
        except Exception as exc:
            ok, result = False, f"{type(exc).__name__}: {exc}"
        self.store.finish(item.id, ok, result, actor="applier",
                          resolve=item.kind in appliers.RESOLVES_ON_APPLY)
        self.log(f"QUEUE applied {item.kind} {item.id} {item.verb}: ok={ok} {result[:160]}")

    def _kick(self, bot) -> None:
        task = asyncio.create_task(self._apply_then_redraw(bot))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _apply_then_redraw(self, bot) -> None:
        try:
            await self.apply_pending(bot)
            for rendered, item in self.store.stale_renders():
                await self._redraw(bot, rendered.chat_id, rendered.message_id, item)
        except Exception as exc:
            self.log(f"QUEUE apply after decision failed: {type(exc).__name__}: {exc}")

    # ---------- Telegram handlers ----------

    def _authorized(self, user) -> bool:
        return user is not None and user.id in self.chat_ids()

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        if not self._authorized(query.from_user):
            self.log(f"QUEUE callback unauthorized: user={query.from_user.id if query.from_user else '?'}")
            await query.answer()
            return
        parsed = render.parse_callback(query.data)
        if parsed is None:
            await query.answer()
            return
        item_id, action = parsed
        item = self.store.get(item_id)
        if item is None:
            await query.answer("That item no longer exists.")
            return
        actor = f"telegram:{query.from_user.id}"
        message = query.message
        chat_id = message.chat.id if message is not None else None
        message_id = message.message_id if message is not None else None

        if action == render.OPEN:
            await query.answer()
            if chat_id is not None:
                await self._send_card(context.bot, chat_id, item)
            return

        async def redraw(it: st.Item) -> None:
            if chat_id is not None:
                await self._redraw(context.bot, chat_id, message_id, it)

        if action == render.SHOW:
            await query.answer()
            await redraw(item)
            return

        if action == render.ANSWER:
            if item.kind != st.QUESTION or item.state not in (st.OPEN, st.SNOOZED):
                await query.answer("Already handled.")
                await redraw(item)
                return
            await query.answer("Reply with your answer.")
            if chat_id is not None:
                try:
                    prompt = await context.bot.send_message(
                        chat_id=chat_id, text=render.answer_prompt_text(item),
                        parse_mode=ParseMode.HTML,
                        reply_markup=ForceReply(selective=True, input_field_placeholder="Your answer"))
                    self.store.add_prompt(item.id, chat_id, prompt.message_id)
                except TelegramError as exc:
                    self.log(f"QUEUE answer prompt failed for {item.id}: {exc}")
            return

        if action in (render.APPROVE, render.REJECT) and not render.needs_confirm(item.kind, action):
            verb = st.APPROVE if action == render.APPROVE else st.REJECT
            if not self.store.decide(item.id, verb, actor=actor):
                await query.answer("Already handled.")
            else:
                await query.answer("Recorded.")
                self.log(f"QUEUE decided {item.kind} {item.id}: {verb}")
                self._kick(context.bot)
            await redraw(self.store.get(item.id))
            return

        if action in (render.APPROVE, render.REJECT):
            if item.state != st.OPEN:
                await query.answer("Already handled.")
                await redraw(item)
                return
            if item.kind == st.PR_REVIEW and action == render.APPROVE and not render.pr_mergeable(item):
                await query.answer("Merge appears once the checks pass.")
                await redraw(item)
                return
            await query.answer()
            if chat_id is not None:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id, message_id=message_id,
                        text=render.confirm_text(item, action), parse_mode=ParseMode.HTML,
                        reply_markup=_markup(render.confirm_keyboard(item, action)),
                        link_preview_options=_NO_PREVIEW)
                except TelegramError as exc:
                    if not _not_modified(exc):
                        self.log(f"QUEUE confirm prompt failed for {item.id}: {exc}")
            return

        if action in (render.APPROVE_CONFIRMED, render.REJECT_CONFIRMED, render.ACK):
            verb = {render.APPROVE_CONFIRMED: st.APPROVE, render.REJECT_CONFIRMED: st.REJECT,
                    render.ACK: st.ACKNOWLEDGE}[action]
            data = None
            if item.kind == st.PR_REVIEW and verb == st.APPROVE:
                # Merge exactly the commit the confirm screen showed. A push or
                # a check change since then means Andy has not seen this one.
                head = str(item.payload.get("head_sha") or "")
                shown = render.shown_head(getattr(message, "text", None))
                if not render.pr_mergeable(item) or not head or shown != head[:7]:
                    await query.answer("The PR changed since that screen. Look again.")
                    await redraw(item)
                    return
                data = {"approved_sha": head}
            if not self.store.decide(item.id, verb, actor=actor, data=data):
                await query.answer("Already handled.")
            else:
                await query.answer("Recorded.")
                self.log(f"QUEUE decided {item.kind} {item.id}: {verb}")
                self._kick(context.bot)
            await redraw(self.store.get(item.id))
            return

        if action == render.SNOOZE:
            ok = self.store.snooze(item.id, int(time.time()) + render.SNOOZE_SECONDS, actor=actor)
            await query.answer("Snoozed for a day." if ok else "Already handled.")
            await redraw(self.store.get(item.id))
            return

        if action == render.RETRY:
            ok = self.store.retry(item.id, actor=actor)
            await query.answer("Retrying." if ok else "Nothing to retry.")
            if ok:
                self._kick(context.bot)
            await redraw(self.store.get(item.id))
            return

        await query.answer()

    async def on_reply(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """A reply to a question's card or answer prompt is the answer. Any
        other message returns untouched, and Luna's handler takes it."""
        message = update.message
        if message is None or message.reply_to_message is None or not message.text:
            return
        if not self._authorized(update.effective_user):
            return
        chat_id = message.chat.id
        item_id = (self.store.prompt_item(chat_id, message.reply_to_message.message_id)
                   or self._question_named_in(message.reply_to_message, context.bot))
        item = self.store.get(item_id) if item_id else None
        if item is None or item.kind != st.QUESTION:
            return
        actor = f"telegram:{update.effective_user.id}"
        if self.store.decide(item.id, st.ANSWER, actor=actor, data={"answer": message.text}):
            self.log(f"QUEUE answered question {item.id} ({len(message.text)} chars)")
            await message.reply_text(f"Saved for {item.agent or 'the agent'}.")
            self._kick(context.bot)
        else:
            await message.reply_text("That question was already handled.")
        raise ApplicationHandlerStop

    def _question_named_in(self, replied_to, bot) -> str | None:
        """The question a reply is for, read from the bot's own message.

        Every card and answer prompt shows its item id. When the message's id
        was never recorded (Telegram delivered the prompt but the send timed
        out, as on 2026-10-12), the id in its text still finds the question.
        Only this bot's messages count, so quoting an id cannot answer one.
        """
        sender = getattr(replied_to, "from_user", None)
        bot_id = getattr(bot, "id", None)
        if sender is None or not getattr(sender, "is_bot", False) or (bot_id and sender.id != bot_id):
            return None
        text = getattr(replied_to, "text", None) or getattr(replied_to, "caption", None) or ""
        for candidate in reversed(re.findall(r"\b[0-9a-f]{6}\b", text)):
            item = self.store.get(candidate)
            if item is not None and item.kind == st.QUESTION:
                return candidate
        return None

    async def cmd_queue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update.effective_user) or update.message is None:
            return
        waiting = self.store.items(states=st.WAITING)
        await update.message.reply_text(
            render.list_text(waiting, limit=LIST_LIMIT), parse_mode=ParseMode.HTML,
            reply_markup=_markup(render.list_keyboard(waiting, limit=LIST_LIMIT)),
            link_preview_options=_NO_PREVIEW)


def _default_appliers(repo: Path) -> dict[str, Applier]:
    return appliers.defaults(repo)
