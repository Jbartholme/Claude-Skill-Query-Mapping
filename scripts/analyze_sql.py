#!/usr/bin/env python3
"""Parse a T-SQL script into a source-lineage graph plus DRY duplicate-detection data.

Uses sqlglot to build a real AST (regex-guessing table names is unreliable once
CTEs, derived tables, and self-joins are involved). For each statement in the
script it walks FROM/JOIN sources recursively, producing:

  - nodes/edges describing how base tables flow through CTEs/subqueries to the
    final result (a layered DAG, source tables at layer 0)
  - table_usage: how many times each base table is referenced in the statement,
    so repeated references (a candidate for consolidating into one CTE) stand out
  - duplicate_logic: near-identical CTE/subquery bodies (by normalized-SQL
    similarity), the actual "you already wrote this" DRY signal

Output is JSON on stdout (or to --output), meant to be fed into build_diagram.py.
"""
import argparse
import difflib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import sqlglot
from sqlglot import exp

DUPLICATE_SIMILARITY_THRESHOLD = 0.90

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def clean_error(e: Exception) -> str:
    """sqlglot's parse errors embed ANSI color codes to underline the bad
    token in a terminal; strip them so the message is readable in the HTML
    warning banner, and collapse it to one line."""
    msg = _ANSI_RE.sub("", str(e))
    return " ".join(msg.split())


def split_batches(sql_text: str):
    """Split on GO batch separators. GO isn't standard SQL/ANSI, it's a T-SQL
    client convention, so sqlglot's parser doesn't know about it - split first."""
    batches = re.split(r"^\s*GO\s*;?\s*$", sql_text, flags=re.IGNORECASE | re.MULTILINE)
    return [b.strip() for b in batches if b.strip()]


def table_label(table: exp.Table) -> str:
    parts = [p for p in [table.catalog, table.db, table.name] if p]
    return ".".join(parts)


def normalize_sql(node: exp.Expression) -> str:
    try:
        return node.sql(dialect="tsql", normalize=True, pretty=False).lower()
    except Exception:
        return node.sql(dialect="tsql", pretty=False).lower()


class GraphBuilder:
    def __init__(self):
        self.nodes = {}
        self.edges = []
        self.table_refs = []  # [(label, target_id)]
        self._auto = 0

    def add_node(self, node_id, **kwargs):
        if node_id not in self.nodes:
            self.nodes[node_id] = {"id": node_id, **kwargs}
        return self.nodes[node_id]

    def add_edge(self, source, target):
        e = {"source": source, "target": target}
        if e not in self.edges:
            self.edges.append(e)

    def auto_alias(self):
        self._auto += 1
        return f"derived_{self._auto}"


def get_arg(expr: exp.Expression, name: str):
    """sqlglot has renamed some Select arg keys across versions (e.g. "from" ->
    "from_", "with" -> "with_"). Check both so this doesn't silently break on
    a version bump."""
    return expr.args.get(name) if expr.args.get(name) is not None else expr.args.get(f"{name}_")


def get_sources(select_expr: exp.Expression):
    """Direct FROM + JOIN sources of a single SELECT (not recursing into subqueries)."""
    sources = []
    from_ = get_arg(select_expr, "from")
    if from_ is not None:
        sources.append(from_.this)
    for j in get_arg(select_expr, "joins") or []:
        sources.append(j.this)
    return sources


def connect_source(gb: GraphBuilder, src, target_id, ctes_in_scope):
    if isinstance(src, exp.Table):
        key = src.name.lower()
        if key in ctes_in_scope:
            gb.add_edge(ctes_in_scope[key], target_id)
        else:
            label = table_label(src)
            table_id = f"table:{label.lower()}"
            gb.add_node(table_id, type="table", label=label)
            gb.add_edge(table_id, target_id)
            gb.table_refs.append((label, target_id))
    elif isinstance(src, exp.Subquery):
        inner = src.this
        alias = src.alias or gb.auto_alias()
        sub_id = f"subquery:{alias}_{id(src)}"
        gb.add_node(sub_id, type="subquery", label=f"({alias})", sql=normalize_sql(inner))
        gb.add_edge(sub_id, target_id)
        process_select(gb, inner, sub_id, ctes_in_scope)
    elif isinstance(src, (exp.Select, exp.Union)):
        connect_sources(gb, src, target_id, ctes_in_scope)
    # anything else (table-valued function, PIVOT, etc.) is left unconnected -
    # not a table/CTE/subquery source, so it doesn't belong in the lineage graph


def connect_sources(gb: GraphBuilder, select_or_union, target_id, ctes_in_scope):
    if isinstance(select_or_union, exp.Union):
        connect_sources(gb, select_or_union.left, target_id, ctes_in_scope)
        connect_sources(gb, select_or_union.right, target_id, ctes_in_scope)
        return
    if not isinstance(select_or_union, exp.Select):
        return
    for src in get_sources(select_or_union):
        connect_source(gb, src, target_id, ctes_in_scope)


def process_select(gb: GraphBuilder, select_expr, node_id, ctes_in_scope):
    """Register this SELECT's own WITH clause (if any) as CTE nodes, then wire
    its FROM/JOIN sources into node_id."""
    local_ctes = dict(ctes_in_scope)
    target = select_expr
    with_ = get_arg(select_expr, "with")

    if with_:
        for cte in with_.expressions:
            alias = cte.alias
            cte_id = f"cte:{alias}"
            gb.add_node(cte_id, type="cte", label=alias, sql=normalize_sql(cte.this))
            local_ctes[alias.lower()] = cte_id
        for cte in with_.expressions:
            cte_id = local_ctes[cte.alias.lower()]
            connect_sources(gb, cte.this, cte_id, local_ctes)

    connect_sources(gb, target, node_id, local_ctes)


def unwrap_statement(stmt: exp.Expression):
    """Find the SELECT/UNION at the heart of a statement (handles INSERT...SELECT,
    CREATE VIEW...AS SELECT, CREATE TABLE...AS SELECT). Returns None for statements
    with no query to map (e.g. plain DDL)."""
    if isinstance(stmt, (exp.Select, exp.Union)):
        return stmt
    inner = stmt.args.get("expression")
    if isinstance(inner, (exp.Select, exp.Union)):
        return inner
    return stmt.find(exp.Select) or stmt.find(exp.Union)


def compute_layers(gb: GraphBuilder):
    predecessors = defaultdict(list)
    for e in gb.edges:
        predecessors[e["target"]].append(e["source"])

    cache = {}
    visiting = set()

    def depth(node_id):
        if node_id in cache:
            return cache[node_id]
        node = gb.nodes.get(node_id)
        if node is None:
            return 0
        if node["type"] == "table":
            cache[node_id] = 0
            return 0
        if node_id in visiting:
            # recursive CTE referencing itself - break the cycle rather than recurse forever
            return 0
        visiting.add(node_id)
        preds = predecessors.get(node_id, [])
        d = 1 + max((depth(p) for p in preds), default=0)
        visiting.discard(node_id)
        cache[node_id] = d
        return d

    for nid in list(gb.nodes):
        depth(nid)
    return cache


def find_duplicate_logic(body_nodes, threshold=DUPLICATE_SIMILARITY_THRESHOLD):
    """body_nodes: list of (id, normalized_sql). Flags near-identical bodies -
    the concrete signal that two CTEs/subqueries should be consolidated into one."""
    results = []
    for i in range(len(body_nodes)):
        for j in range(i + 1, len(body_nodes)):
            id_a, sql_a = body_nodes[i]
            id_b, sql_b = body_nodes[j]
            if not sql_a or not sql_b:
                continue
            ratio = difflib.SequenceMatcher(None, sql_a, sql_b).ratio()
            if ratio >= threshold:
                results.append({
                    "a": id_a,
                    "b": id_b,
                    "similarity": round(ratio, 3),
                    "exact": sql_a == sql_b,
                })
    return sorted(results, key=lambda r: -r["similarity"])


def analyze(sql_text: str, dialect: str = "tsql", source_name: str = "query"):
    batches = split_batches(sql_text) or [sql_text]
    warnings = []
    statements_out = []
    global_body_nodes = []  # [(prefixed_id, normalized_sql)]
    global_table_refs = []  # [label]
    stmt_index = 0

    for batch in batches:
        try:
            parsed = sqlglot.parse(batch, dialect=dialect)
        except Exception as e:
            warnings.append(f"Could not parse a batch: {clean_error(e)}")
            continue

        for stmt in parsed:
            if stmt is None:
                continue
            main = unwrap_statement(stmt)
            if main is None:
                continue  # e.g. plain DDL with no query body - nothing to map

            stmt_index += 1
            gb = GraphBuilder()
            output_id = "output"
            gb.add_node(output_id, type="output", label="Result")

            try:
                process_select(gb, main, output_id, {})
            except Exception as e:
                warnings.append(f"Statement {stmt_index}: could not build graph ({clean_error(e)})")
                stmt_index -= 1
                continue

            layers = compute_layers(gb)
            for nid, layer in layers.items():
                gb.nodes[nid]["layer"] = layer

            usage_counter = Counter(label for label, _ in gb.table_refs)
            table_usage = [
                {"table": label, "count": count, "duplicate": count > 1}
                for label, count in sorted(usage_counter.items(), key=lambda x: (-x[1], x[0]))
            ]

            body_nodes = [(nid, n.get("sql")) for nid, n in gb.nodes.items() if n.get("sql")]
            duplicate_logic = find_duplicate_logic(body_nodes)

            statements_out.append({
                "index": stmt_index,
                "nodes": list(gb.nodes.values()),
                "edges": gb.edges,
                "table_usage": table_usage,
                "duplicate_logic": duplicate_logic,
            })

            global_table_refs.extend(label for label, _ in gb.table_refs)
            global_body_nodes.extend((f"stmt{stmt_index}:{nid}", sql) for nid, sql in body_nodes)

    overall_usage_counter = Counter(global_table_refs)
    overall_table_usage = [
        {"table": label, "count": count, "duplicate": count > 1}
        for label, count in sorted(overall_usage_counter.items(), key=lambda x: (-x[1], x[0]))
    ]
    cross_statement_duplicate_logic = (
        find_duplicate_logic(global_body_nodes) if stmt_index > 1 else []
    )

    return {
        "source": source_name,
        "dialect": dialect,
        "statement_count": stmt_index,
        "warnings": warnings,
        "statements": statements_out,
        "overall_table_usage": overall_table_usage,
        "cross_statement_duplicate_logic": cross_statement_duplicate_logic,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", help="Path to a .sql file; reads stdin if omitted")
    parser.add_argument("--dialect", default="tsql", help="sqlglot dialect (default: tsql)")
    parser.add_argument("-o", "--output", help="Write JSON here instead of stdout")
    args = parser.parse_args()

    if args.input:
        sql_text = Path(args.input).read_text()
        source_name = Path(args.input).name
    else:
        sql_text = sys.stdin.read()
        source_name = "pasted_query"

    result = analyze(sql_text, dialect=args.dialect, source_name=source_name)
    out = json.dumps(result, indent=2)

    if args.output:
        Path(args.output).write_text(out)
    else:
        print(out)


if __name__ == "__main__":
    main()
