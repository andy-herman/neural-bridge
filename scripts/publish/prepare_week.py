"""Sunday-evening prep job for the weekly publishing cadence.

Runs under launchd every Sunday at 18:00 PT. Workflow:

  1. Find the next eligible draft in the blog repo (~/Development/neural-bridge-blog).
     Eligible = `draft: true` AND `pubDate <= upcoming Monday (UTC)`.
     If multiple match, pick the oldest pubDate (FIFO).
  2. Generate a LinkedIn variant via `claude -p` using the voice corpus at
     `<vault>/Neural Bridge/Voice/linkedin-andy.md`.
  3. Generate a deterministic X draft (same shape as the existing tweet-on-publish
     workflow's draft-tweet.mjs).
  4. Write both to `<vault>/Neural Bridge/Drafts/scheduled/<monday>/<slug>-{linkedin,x}.md`.
  5. Post a Discord briefing summarizing the three artifacts.

If no draft is eligible, post a "nothing queued" message and exit clean.

Idempotency: if outputs already exist for the upcoming Monday's slug, the
LinkedIn generation step is skipped (it costs a Claude call). Pass `--force`
to regenerate.

Manual invocation:
  python -m scripts.publish.prepare_week                         # use today's local date
  python -m scripts.publish.prepare_week --for-monday 2026-05-18 # override target Monday
  python -m scripts.publish.prepare_week --dry-run               # plan-only, no writes
  python -m scripts.publish.prepare_week --force                 # regenerate even if cached
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Repo-relative imports work because we run as `python -m scripts.publish.prepare_week`
# from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from hooks import discord_post  # noqa: E402
from scripts.discord_bot.claude_invoke import call_claude_sync  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BLOG_REPO = Path.home() / "Development" / "neural-bridge-blog"
BLOG_CONTENT_DIRS = [
    BLOG_REPO / "src" / "content" / "posts",
    BLOG_REPO / "src" / "content" / "research",
]
SITE_ORIGIN = "https://neural-bridge.dev"

VAULT_ROOT = Path.home() / "Documents" / "Luna Master"
VOICE_CORPUS = VAULT_ROOT / "Neural Bridge" / "Voice" / "linkedin-andy.md"
SCHEDULED_DRAFTS = VAULT_ROOT / "Neural Bridge" / "Drafts" / "scheduled"

LINKEDIN_PROMPT_TEMPLATE = REPO_ROOT / "scripts" / "publish" / "prompts" / "linkedin_v1.md"

CLAUDE_TIMEOUT = 600  # 10 min — long-form generation

URL_TWITTER_LENGTH = 23
MAX_TWEET = 280


@dataclass
class Candidate:
    file_path: Path
    slug: str
    title: str
    description: str
    pub_date: date
    content_type: str  # "posts" or "research"
    body: str


def parse_frontmatter(raw: str) -> tuple[dict, str]:
    """Minimal YAML frontmatter parser. Same shape as scheduled-publish.mjs.
    Returns (data, body)."""
    if not raw.startswith("---\n") and not raw.startswith("---\r\n"):
        return {}, raw
    lines = raw.split("\n")
    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r") == "---":
            end_idx = i
            break
    if end_idx == -1:
        return {}, raw

    fm_lines = lines[1:end_idx]
    body = "\n".join(lines[end_idx + 1:])

    data: dict = {}
    i = 0
    while i < len(fm_lines):
        line = fm_lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        colon_idx = line.find(":")
        if colon_idx == -1:
            i += 1
            continue
        key = line[:colon_idx].strip()
        value = line[colon_idx + 1:].strip()

        # Skip multiline block scalars; we don't need them for the keys we read
        if value in ("|", "|-", "|+", ">", ">-"):
            i += 1
            while i < len(fm_lines) and (fm_lines[i].startswith("  ") or fm_lines[i] == ""):
                i += 1
            continue

        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]

        if key == "draft":
            data[key] = value.lower() == "true"
        else:
            data[key] = value
        i += 1
    return data, body


def upcoming_monday_utc(today_utc: date | None = None) -> date:
    """Return the next Monday in UTC. If today (UTC) is already a Monday, return today."""
    if today_utc is None:
        today_utc = datetime.now(timezone.utc).date()
    weekday = today_utc.weekday()  # Monday = 0
    days_ahead = (0 - weekday) % 7
    return today_utc + timedelta(days=days_ahead)


def parse_pub_date(raw: str) -> date | None:
    """Accept '2026-05-18' or '2026-05-18T00:00:00Z' etc. Return UTC date."""
    if not raw:
        return None
    # Strip quotes that might have leaked through
    raw = raw.strip().strip('"').strip("'")
    # Take just the date portion
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", raw)
    if not m:
        return None
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def slug_from_filename(path: Path) -> str:
    return path.stem


def find_candidates(target_monday: date) -> list[Candidate]:
    """Find all eligible candidates: draft: true AND pubDate <= target_monday."""
    out: list[Candidate] = []
    for d in BLOG_CONTENT_DIRS:
        if not d.exists():
            continue
        content_type = d.name  # "posts" or "research"
        for p in sorted(d.iterdir()):
            if not p.is_file() or p.suffix not in (".md", ".mdx"):
                continue
            raw = p.read_text(encoding="utf-8")
            fm, body = parse_frontmatter(raw)
            if not fm.get("draft"):
                continue
            pd = parse_pub_date(fm.get("pubDate", ""))
            if pd is None or pd > target_monday:
                continue
            out.append(Candidate(
                file_path=p,
                slug=slug_from_filename(p),
                title=fm.get("title", p.stem),
                description=fm.get("description", ""),
                pub_date=pd,
                content_type=content_type,
                body=body,
            ))
    # Oldest pubDate first (FIFO).
    out.sort(key=lambda c: (c.pub_date, c.file_path.name))
    return out


def build_linkedin_prompt(candidate: Candidate) -> str:
    template = LINKEDIN_PROMPT_TEMPLATE.read_text(encoding="utf-8")
    voice = VOICE_CORPUS.read_text(encoding="utf-8") if VOICE_CORPUS.exists() else ""
    return (template
            .replace("{voice_corpus}", voice)
            .replace("{title}", candidate.title)
            .replace("{description}", candidate.description)
            .replace("{blog_body}", candidate.body))


def build_x_draft(candidate: Candidate) -> str:
    """Mirror the format of .github/scripts/draft-tweet.mjs in the blog repo."""
    prefix = "📄 New research" if candidate.content_type == "research" else "🆕 New post"
    title_line = f"{prefix}: {candidate.title}"
    url = f"{SITE_ORIGIN}/{candidate.content_type}/{candidate.slug}"
    overhead = len(title_line) + 4 + URL_TWITTER_LENGTH  # 4 for "\n\n" x2
    body_max = MAX_TWEET - overhead
    desc = candidate.description or ""
    if len(desc) > body_max:
        desc = desc[: max(0, body_max - 1)].rstrip() + "…"
    return f"{title_line}\n\n{desc}\n\n{url}"


def discord_brief(candidate: Candidate, target_monday: date,
                  linkedin_path: Path, x_path: Path) -> str:
    rel_blog = candidate.file_path.relative_to(BLOG_REPO)
    rel_linkedin = linkedin_path.relative_to(VAULT_ROOT)
    rel_x = x_path.relative_to(VAULT_ROOT)
    monday_iso = target_monday.isoformat()
    return (
        f"📅 **Sunday brief — publishing Monday {monday_iso}**\n\n"
        f"📝 **{candidate.title}**\n"
        f"_{candidate.description}_\n\n"
        f"Three artifacts ready for review in Obsidian:\n"
        f"• 📰 Blog: `neural-bridge-blog/{rel_blog}`\n"
        f"• 💼 LinkedIn: `{rel_linkedin}`\n"
        f"• 🐦 X: `{rel_x}`\n\n"
        f"Edit any of them in the vault. Monday 18:00 PT, the blog cron flips "
        f"`draft: true` → published and Vercel auto-deploys. To skip this week, "
        f"change `pubDate` in the blog file's frontmatter to a future Monday."
    )


def discord_no_queue(target_monday: date) -> str:
    return (
        f"📅 **Sunday brief — no publish queued for Monday {target_monday.isoformat()}**\n\n"
        f"No drafts in `~/Development/neural-bridge-blog/src/content/{{posts,research}}/` "
        f"have `draft: true` and `pubDate <= {target_monday.isoformat()}`. "
        f"Mark a draft ready by setting those fields in its frontmatter."
    )


def generate_linkedin_variant(candidate: Candidate, *, dry_run: bool) -> tuple[bool, str]:
    """Returns (ok, content_or_error)."""
    if not VOICE_CORPUS.exists():
        return False, f"voice corpus missing at {VOICE_CORPUS}"
    if not LINKEDIN_PROMPT_TEMPLATE.exists():
        return False, f"prompt template missing at {LINKEDIN_PROMPT_TEMPLATE}"
    prompt = build_linkedin_prompt(candidate)
    if dry_run:
        return True, f"<dry-run: would call claude with prompt of {len(prompt)} chars>"
    ok, stdout, err = call_claude_sync(prompt, timeout=CLAUDE_TIMEOUT)
    if not ok:
        return False, f"claude call failed: {err}"
    return True, stdout.strip()


def write_artifact(path: Path, content: str, *, dry_run: bool) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def post_discord(message: str, *, dry_run: bool) -> bool:
    if dry_run:
        return True
    return discord_post.send(message)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sunday prep for the weekly publish.")
    parser.add_argument("--for-monday", type=str, default=None,
                        help="Target Monday in YYYY-MM-DD; default = next Monday in UTC.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Plan only; do not call Claude, write files, or post.")
    parser.add_argument("--force", action="store_true",
                        help="Regenerate LinkedIn variant even if a cached file exists.")
    args = parser.parse_args(argv)

    target_monday = (
        date.fromisoformat(args.for_monday) if args.for_monday
        else upcoming_monday_utc()
    )
    print(f"target_monday = {target_monday.isoformat()}", file=sys.stderr)

    candidates = find_candidates(target_monday)
    if not candidates:
        msg = discord_no_queue(target_monday)
        print(msg, file=sys.stderr)
        post_discord(msg, dry_run=args.dry_run)
        return 0

    candidate = candidates[0]
    print(f"selected: {candidate.file_path.relative_to(BLOG_REPO)} (pubDate={candidate.pub_date})",
          file=sys.stderr)

    week_dir = SCHEDULED_DRAFTS / target_monday.isoformat()
    linkedin_path = week_dir / f"{candidate.slug}-linkedin.md"
    x_path = week_dir / f"{candidate.slug}-x.md"

    # LinkedIn variant.
    if linkedin_path.exists() and not args.force:
        print(f"linkedin already exists, skipping generation: {linkedin_path}", file=sys.stderr)
    else:
        ok, content = generate_linkedin_variant(candidate, dry_run=args.dry_run)
        if not ok:
            err_msg = (
                f"⚠️ Sunday prep failed for **{candidate.title}**: "
                f"LinkedIn variant generation error: `{content}`"
            )
            print(err_msg, file=sys.stderr)
            post_discord(err_msg, dry_run=args.dry_run)
            return 1
        write_artifact(linkedin_path, content + "\n", dry_run=args.dry_run)

    # X draft (deterministic, no Claude).
    if x_path.exists() and not args.force:
        print(f"x already exists, skipping: {x_path}", file=sys.stderr)
    else:
        write_artifact(x_path, build_x_draft(candidate) + "\n", dry_run=args.dry_run)

    # Discord briefing.
    brief = discord_brief(candidate, target_monday, linkedin_path, x_path)
    print(brief, file=sys.stderr)
    posted = post_discord(brief, dry_run=args.dry_run)
    if not posted and not args.dry_run:
        print("WARN: discord post returned False (webhook missing or HTTP error)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
