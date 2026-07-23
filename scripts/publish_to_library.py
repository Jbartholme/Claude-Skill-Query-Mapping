#!/usr/bin/env python3
"""Publish an analyzed query into a shared, git-trackable library so a team
can browse each other's mapped queries and spot duplication across *separate*
scripts, not just within one.

Copies the analysis JSON + diagram HTML into <library-dir>/<slug>/, appends
an entry to <library-dir>/entries.json (the running manifest), and
regenerates <library-dir>/index.html - a self-contained page listing every
analyzed query (title, author, date, score, tables touched) plus a
cross-script table index: which tables show up in more than one script,
the clearest signal that two people built overlapping logic separately.

Run this from inside whatever repo the team already shares (their SQL
scripts repo) so index.html and library/ get committed alongside it - not
from inside this skill's own directory.
"""
import argparse
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "assets" / "library_index_template.html"


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "query"


def unique_slug(base: str, library_dir: Path) -> str:
    slug = base
    n = 2
    while (library_dir / slug).exists():
        slug = f"{base}-{n}"
        n += 1
    return slug


def summarize(analysis: dict) -> dict:
    statements = analysis.get("statements", [])
    worst_score = min((s["score"]["value"] for s in statements), default=100)
    duplicate_logic_count = sum(s.get("duplicate_logic_total", len(s.get("duplicate_logic", []))) for s in statements)
    duplicate_logic_count += analysis.get("cross_statement_duplicate_logic_total", 0)
    overall_tables = analysis.get("overall_table_usage", [])
    tables = [t["table"] for t in overall_tables]
    duplicate_table_count = sum(1 for t in overall_tables if t["duplicate"])
    temp_findings = len(analysis.get("temp_object_findings", []))
    return {
        "worst_score": worst_score,
        "statement_count": analysis.get("statement_count", len(statements)),
        "tables": tables,
        "duplicate_table_count": duplicate_table_count,
        "duplicate_logic_count": duplicate_logic_count,
        "temp_object_finding_count": temp_findings,
    }


def load_entries(library_dir: Path) -> list:
    manifest = library_dir / "entries.json"
    if manifest.exists():
        return json.loads(manifest.read_text()).get("entries", [])
    return []


def save_entries(library_dir: Path, entries: list):
    manifest = library_dir / "entries.json"
    manifest.write_text(json.dumps({"entries": entries}, indent=2))


def build_index_html(entries: list) -> str:
    template = TEMPLATE_PATH.read_text()
    payload = json.dumps(entries)
    payload = payload.replace("</script>", "<\\/script>")
    return template.replace("__ENTRIES__", payload)


def publish(analysis_path: Path, diagram_path: Path, library_dir: Path, title: str = None,
            author: str = None, notes: str = None):
    analysis = json.loads(analysis_path.read_text())
    summary = summarize(analysis)

    title = title or analysis.get("source", "query")
    base_slug = slugify(title)
    library_dir.mkdir(parents=True, exist_ok=True)
    slug = unique_slug(base_slug, library_dir)

    entry_dir = library_dir / slug
    entry_dir.mkdir(parents=True)
    (entry_dir / "analysis.json").write_text(analysis_path.read_text())
    (entry_dir / "diagram.html").write_text(diagram_path.read_text())

    entry = {
        "slug": slug,
        "title": title,
        "source": analysis.get("source", ""),
        "dialect": analysis.get("dialect", ""),
        "author": author or "",
        "notes": notes or "",
        "added_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **summary,
    }

    entries = load_entries(library_dir)
    entries.append(entry)
    save_entries(library_dir, entries)

    index_html = build_index_html(entries)
    (library_dir / "index.html").write_text(index_html)

    return entry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis", required=True, help="Path to analyze_sql.py's JSON output")
    parser.add_argument("--diagram", required=True, help="Path to build_diagram.py's HTML output")
    parser.add_argument("--library-dir", default="library", help="Shared library directory (default: ./library)")
    parser.add_argument("--title", help="Human-readable name for this query (default: source filename)")
    parser.add_argument("--author", help="Who's publishing this (default: blank)")
    parser.add_argument("--notes", help="Short note about what this query is for")
    args = parser.parse_args()

    entry = publish(
        Path(args.analysis), Path(args.diagram), Path(args.library_dir),
        title=args.title, author=args.author, notes=args.notes,
    )
    print(f"Published '{entry['title']}' as {args.library_dir}/{entry['slug']}/")
    print(f"Index: {args.library_dir}/index.html")


if __name__ == "__main__":
    main()
