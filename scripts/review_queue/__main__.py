"""Review queue CLI. The desk surface, and the producers' contract.

For Andy:
    python -m scripts.review_queue list [--all] [--kind KIND]
    python -m scripts.review_queue show ID
    python -m scripts.review_queue decide ID {approve,reject,acknowledge} [--note TEXT]
    python -m scripts.review_queue snooze ID [--hours 24]
    python -m scripts.review_queue retry ID
    python -m scripts.review_queue apply ID          (run a decided item's applier here)
    python -m scripts.review_queue stats [--days 7]
    python -m scripts.review_queue health [--json]

For producers (idempotent; safe to call every run):
    python -m scripts.review_queue raise --source S --key K --kind KIND --title T
                                         [--detail D] [--ref URL] [--agent A]
    python -m scripts.review_queue clear --source S --key K
    python -m scripts.review_queue sync --source S --kind KIND < entries.json
    python -m scripts.review_queue sync-captures

`sync` reads a JSON list of {key, title, detail?, ref?, agent?} on stdin: the
source's complete current set. Anything the source raised before and no
longer lists is cleared.

Exit codes: 0 ok, 1 the request was refused (e.g. already decided), 2 error.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.review_queue import captures, health  # noqa: E402
from scripts.review_queue import store as st  # noqa: E402


def _when(epoch: int | None) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M") if epoch else "-"


def _line(item: st.Item) -> str:
    return (f"{item.id}  {item.kind:<8} {item.state:<12} {_when(item.created_at)}  "
            f"{item.source}: {item.title}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.review_queue",
                                     description="Neural Bridge review queue")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="items waiting on a decision")
    p.add_argument("--all", action="store_true", help="every item, not just waiting ones")
    p.add_argument("--kind", choices=sorted(st.KINDS))

    p = sub.add_parser("show", help="one item and its event log")
    p.add_argument("id")

    p = sub.add_parser("decide", help="record a decision")
    p.add_argument("id")
    p.add_argument("verb", choices=[st.APPROVE, st.REJECT, st.ACKNOWLEDGE])
    p.add_argument("--note", default="")

    p = sub.add_parser("snooze", help="hide an open item for a while")
    p.add_argument("id")
    p.add_argument("--hours", type=float, default=24)

    p = sub.add_parser("retry", help="re-run a failed apply")
    p.add_argument("id")

    p = sub.add_parser("apply", help="run a decided item's applier in this process")
    p.add_argument("id")

    p = sub.add_parser("stats", help="decisions, time to decision, producer volume")
    p.add_argument("--days", type=float, default=7)

    p = sub.add_parser("health", help="read-back check; exit 1 when unhealthy")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("raise", help="producer: create an item unless it exists")
    p.add_argument("--source", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--kind", required=True, choices=sorted(st.KINDS - {st.PROBE}))
    p.add_argument("--title", required=True)
    p.add_argument("--detail", default="")
    p.add_argument("--ref", default="")
    p.add_argument("--agent", default="")

    p = sub.add_parser("clear", help="producer: the condition is gone")
    p.add_argument("--source", required=True)
    p.add_argument("--key", required=True)

    p = sub.add_parser("sync", help="producer: make the source's live items match stdin")
    p.add_argument("--source", required=True)
    p.add_argument("--kind", required=True, choices=sorted(st.KINDS - {st.PROBE}))

    sub.add_parser("sync-captures", help="queue every quarantine file committed on main")

    args = parser.parse_args(argv)
    store = st.Store()
    actor = "cli"

    if args.cmd == "list":
        items = store.items() if args.all else store.items(states=st.WAITING)
        if args.kind:
            items = [i for i in items if i.kind == args.kind]
        for item in items:
            print(_line(item))
        if not items:
            print("Nothing is waiting." if not args.all else "The queue is empty.")
        return 0

    if args.cmd == "show":
        item = store.get(args.id)
        if item is None:
            print(f"no item {args.id}", file=sys.stderr)
            return 2
        print(_line(item))
        for label, value in (("agent", item.agent), ("detail", item.detail), ("ref", item.ref),
                             ("verb", item.verb), ("result", item.result),
                             ("payload", json.dumps(item.payload) if item.payload else "")):
            if value:
                print(f"  {label}: {value}")
        for e in store.events(item.id):
            print(f"  {_when(e['at'])}  {e['actor']:<18} {e['event']:<14} {e['detail']}")
        return 0

    if args.cmd == "decide":
        ok = store.decide(args.id, args.verb, actor=actor, note=args.note)
        print("recorded; Luna's bridge applies it within a minute" if ok
              else "not waiting on a decision (already decided, cleared, or unknown)")
        return 0 if ok else 1

    if args.cmd == "snooze":
        ok = store.snooze(args.id, int(time.time() + args.hours * 3600), actor=actor)
        print("snoozed" if ok else "only open items can be snoozed")
        return 0 if ok else 1

    if args.cmd == "retry":
        ok = store.retry(args.id, actor=actor)
        print("queued for another apply" if ok else "only failed applies can be retried")
        return 0 if ok else 1

    if args.cmd == "apply":
        item = store.get(args.id)
        if item is None or not store.claim(args.id, actor=actor):
            print("only a decided item can be applied (see `show`)", file=sys.stderr)
            return 1
        if item.kind == st.CAPTURE:
            ok, result = captures.apply(item, repo=REPO_ROOT)
        elif item.kind in (st.ALERT, st.PROBE):
            ok, result = True, ""
        else:
            ok, result = False, f"no applier for {item.kind} items yet"
        store.finish(args.id, ok, result, actor=actor, resolve=item.kind == st.CAPTURE)
        print(("applied" if ok else "failed") + (f": {result}" if result else ""))
        return 0 if ok else 1

    if args.cmd == "stats":
        print(json.dumps(store.stats(since=int(time.time() - args.days * 86400)), indent=2))
        return 0

    if args.cmd == "health":
        result = health.check(store, health.push_kinds())
        print(json.dumps(result, indent=2) if args.json else health.format_check(result))
        return 0 if result["ok"] else 1

    if args.cmd == "raise":
        item, created = store.raise_item(source=args.source, key=args.key, kind=args.kind,
                                         title=args.title, detail=args.detail, ref=args.ref,
                                         agent=args.agent)
        print(f"{item.id} {'created' if created else 'exists'}")
        return 0

    if args.cmd == "clear":
        item = store.clear(source=args.source, key=args.key)
        print(f"{item.id} resolved" if item else "nothing live under that key")
        return 0

    if args.cmd == "sync":
        try:
            entries = json.load(sys.stdin)
            if not isinstance(entries, list) or not all(
                    isinstance(e, dict) and "key" in e and "title" in e for e in entries):
                raise ValueError("expected a JSON list of objects with key and title")
        except ValueError as exc:
            print(f"bad input: {exc}", file=sys.stderr)
            return 2
        created, cleared = store.sync(source=args.source, kind=args.kind, entries=entries)
        print(f"created {len(created)}, cleared {len(cleared)}, live {len(entries)}")
        return 0

    if args.cmd == "sync-captures":
        created, cleared = captures.sync(store, REPO_ROOT)
        print(f"created {len(created)}, cleared {len(cleared)}")
        return 0

    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"review queue: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
