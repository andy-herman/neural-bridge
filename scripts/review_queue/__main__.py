"""Review queue CLI. The desk surface, and the producers' contract.

For Andy:
    python -m scripts.review_queue list [--all] [--kind KIND]
    python -m scripts.review_queue show ID
    python -m scripts.review_queue decide ID {approve,reject,acknowledge} [--note TEXT]
    python -m scripts.review_queue answer ID "TEXT"   (a question's answer, into the agent's progress log)
    python -m scripts.review_queue snooze ID [--hours 24]
    python -m scripts.review_queue retry ID
    python -m scripts.review_queue apply ID          (run a decided item's applier here)
    python -m scripts.review_queue stats [--days 7]
    python -m scripts.review_queue health [--json]

The pre-queue question backlog (one triage pass, at the desk):
    python -m scripts.review_queue backlog [--grep TEXT] [--month YYYY-MM] [--all]
    python -m scripts.review_queue backlog keep ID... --agent AGENT
    python -m scripts.review_queue backlog close

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

from scripts.review_queue import appliers, health, questions  # noqa: E402
from scripts.review_queue import captures  # noqa: E402
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

    p = sub.add_parser("answer", help="answer a question; it goes into the agent's progress log")
    p.add_argument("id")
    p.add_argument("text")

    p = sub.add_parser("backlog", help="triage the open questions flush wrote before the queue existed")
    p.add_argument("op", nargs="?", choices=["list", "keep", "close"], default="list")
    p.add_argument("ids", nargs="*", help="backlog ids to keep (from the list)")
    p.add_argument("--agent", default="", help="who a kept question goes to")
    p.add_argument("--grep", default="", help="only questions containing this text")
    p.add_argument("--month", default="", help="only this month, YYYY-MM")
    p.add_argument("--all", action="store_true", help="list even after the backlog was closed")

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

    if args.cmd == "answer":
        item = store.get(args.id)
        if item is None or item.kind != st.QUESTION:
            print(f"no question {args.id}", file=sys.stderr)
            return 2
        ok = store.decide(args.id, st.ANSWER, actor=actor, data={"answer": args.text})
        print(f"recorded; Luna's bridge writes it to {item.agent}'s progress log within a minute" if ok
              else "not waiting on a decision (already handled, or cleared)")
        return 0 if ok else 1

    if args.cmd == "backlog":
        return _backlog(store, args)

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
        applier = appliers.defaults(REPO_ROOT).get(item.kind)
        decided = store.get(args.id) or item
        try:
            ok, result = applier(decided) if applier else (False, f"no applier for {item.kind} items")
        except Exception as exc:
            ok, result = False, f"{type(exc).__name__}: {exc}"
        store.finish(args.id, ok, result, actor=actor, resolve=item.kind in appliers.RESOLVES_ON_APPLY)
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


def _backlog(store: st.Store, args) -> int:
    entries = questions.backlog(REPO_ROOT / "daily-logs")
    kept = {i.key for i in store.items(kinds=(st.QUESTION,)) if i.source == questions.BACKLOG_SOURCE}
    closed = store.meta_get(questions.BACKLOG_CLOSED_KEY)

    if args.op == "keep":
        if not args.ids or not args.agent:
            print("keep needs backlog ids and --agent (who the answer goes to)", file=sys.stderr)
            return 2
        by_id = {e["id"]: e for e in entries}
        missing = [i for i in args.ids if i not in by_id]
        if missing:
            print(f"not in the backlog: {', '.join(missing)}", file=sys.stderr)
            return 2
        for i in args.ids:
            item, created = questions.keep(store, by_id[i], args.agent)
            print(f"{item.id} {'queued' if created else 'already queued'}: {item.title}")
        return 0

    if args.op == "close":
        left = sum(1 for e in entries if e["key"] not in kept)
        store.meta_set(questions.BACKLOG_CLOSED_KEY,
                       json.dumps({"at": int(time.time()), "kept": len(kept), "closed": left}))
        print(f"backlog closed: {len(kept)} kept as questions, {left} left in daily-logs/ unanswered")
        return 0

    if closed and not args.all:
        info = json.loads(closed)
        print(f"The backlog was closed {_when(info['at'])}: {info['kept']} kept, {info['closed']} left. "
              f"--all lists it anyway.")
        return 0
    shown = [e for e in entries if e["key"] not in kept
             and (not args.grep or args.grep.lower() in e["question"].lower())
             and (not args.month or e["day"].startswith(args.month))]
    for e in shown:
        print(f"{e['id']}  {e['day']}  {e['agent']:<14} {e['question'][:160]}")
    print(f"\n{len(shown)} shown, {len(entries)} in the backlog, {len(kept)} kept. "
          f"Keep: backlog keep ID... --agent AGENT. Done: backlog close.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"review queue: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
