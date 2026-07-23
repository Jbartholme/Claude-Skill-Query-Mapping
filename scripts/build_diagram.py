#!/usr/bin/env python3
"""Inject analyze_sql.py's JSON output into assets/diagram_template.html,
producing a self-contained interactive HTML file."""
import argparse
import json
import sys
from pathlib import Path

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "assets" / "diagram_template.html"


def build(data: dict) -> str:
    template = TEMPLATE_PATH.read_text()
    payload = json.dumps(data)
    # a literal "</script>" inside the JSON (e.g. from SQL text) would otherwise
    # close the script tag early and break the page
    payload = payload.replace("</script>", "<\\/script>")
    if "__DATA__" not in template:
        raise RuntimeError("template is missing the __DATA__ placeholder")
    return template.replace("__DATA__", payload)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", help="Path to analyze_sql.py's JSON output; reads stdin if omitted")
    parser.add_argument("-o", "--output", required=True, help="Path to write the HTML file")
    args = parser.parse_args()

    raw = Path(args.input).read_text() if args.input else sys.stdin.read()
    data = json.loads(raw)
    html = build(data)

    out_path = Path(args.output)
    out_path.write_text(html)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
