#!/usr/bin/env python3
"""md_to_pdf.py — render a Markdown file to PDF with CJK (e.g. Korean) support.

Deps (pip): markdown, weasyprint
Usage: python3 scripts/md_to_pdf.py INPUT.md [OUTPUT.pdf]
Default output: the input path with a .pdf extension, written next to the source.

Failure modes:
- Missing pip deps -> exits 1 with an install hint.
- weasyprint native libs (pango/cairo) missing -> import error surfaced verbatim.
- Input not found -> exits 1.

Kept engine-light on purpose: no pandoc, no texlive. CJK renders via a
system font declared in CSS below (Apple SD Gothic Neo on macOS).
"""
import sys
from pathlib import Path

try:
    import markdown
    from weasyprint import HTML
except ImportError as exc:
    sys.exit(
        f"missing dependency: {exc.name}. install with: pip install markdown weasyprint"
    )

CSS = """
@page { size: letter; margin: 0.9in; }
body { font-family: -apple-system, "Helvetica Neue", "Apple SD Gothic Neo", sans-serif;
       font-size: 11pt; line-height: 1.45; color: #111; }
h1 { font-size: 18pt; }
h2 { font-size: 14pt; margin-top: 1.2em; }
blockquote { border-left: 3px solid #ccc; margin-left: 0; padding-left: 12px; color: #333; }
code, pre { font-family: "SF Mono", Menlo, monospace; font-size: 10pt; background: #f5f5f5; }
pre { padding: 10px; border-radius: 4px; white-space: pre-wrap; }
hr { border: none; border-top: 1px solid #ddd; }
"""


def render(src: Path, out: Path) -> None:
    html_body = markdown.markdown(
        src.read_text(encoding="utf-8"),
        extensions=["extra", "sane_lists"],
    )
    html_doc = (
        "<html><head><meta charset='utf-8'>"
        f"<style>{CSS}</style></head><body>{html_body}</body></html>"
    )
    HTML(string=html_doc).write_pdf(str(out))


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: md_to_pdf.py INPUT.md [OUTPUT.pdf]", file=sys.stderr)
        return 1
    src = Path(argv[1]).expanduser()
    if not src.is_file():
        print(f"not found: {src}", file=sys.stderr)
        return 1
    out = Path(argv[2]).expanduser() if len(argv) > 2 else src.with_suffix(".pdf")
    render(src, out)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
