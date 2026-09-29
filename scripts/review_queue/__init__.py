"""The review queue: one place where agent output that needs Andy lands.

Phase 1 of the 2026-08-02 roadmap. The fleet sat dormant because every path
either waited for Andy to start a conversation or pushed a one-way notice he
could not act on. Producers now write items here; Luna's Telegram bot pushes
each one with buttons; every decision and its outcome is logged.

    store.py      SQLite store: items, append-only events, Telegram renders
    captures.py   compile quarantine producer, and the file/reject applier
    render.py     Telegram card text and buttons (pure)
    surface.py    the pusher loop, button callbacks and /queue in Luna's bot
    health.py     read-back check (write, read, decide, confirm the event log)
    __main__.py   CLI: python -m scripts.review_queue --help

See docs/REVIEW_QUEUE.md.
"""
