#!/usr/bin/env python3
"""Parse a T-SQL script into a source-lineage graph, column-level usage, DRY
duplicate-detection data, and a heuristic performance score.

Uses sqlglot to build a real AST (regex-guessing table names is unreliable once
CTEs, derived tables, and self-joins are involved). For each statement in the
script it walks FROM/JOIN sources recursively, producing:

  - nodes/edges describing how base tables flow through CTEs/subqueries to the
    final result (a layered DAG, source tables at layer 0), each node carrying
    the distinct columns actually referenced from it (for "search this source,
    see its columns" on large scripts with dozens of objects)
  - table_usage: how many times each base table is referenced in the statement,
    so repeated references (a candidate for consolidating into one CTE) stand out
  - duplicate_logic: near-identical CTE/subquery bodies (by normalized-SQL
    similarity), the actual "you already wrote this" DRY signal
  - sargability_findings: static red flags that defeat index usage (functions
    wrapping filter/join columns, leading-wildcard LIKE, cartesian joins)
  - temp_object_findings: #temp tables / table variables that get joined or
    filtered on but never indexed
  - score: a 0-100 heuristic estimate combining the above (DRY + index/
    sargability signals weighted heaviest, complexity weighted lightly), with
    an itemized breakdown - NOT a measured execution cost, since there's no
    live database/execution plan to inspect here

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


def get_arg(expr: exp.Expression, name: str):
    """sqlglot has renamed some Select arg keys across versions (e.g. "from" ->
    "from_", "with" -> "with_"). Check both so this doesn't silently break on
    a version bump."""
    return expr.args.get(name) if expr.args.get(name) is not None else expr.args.get(f"{name}_")


def table_label(table: exp.Table, dialect: str = "tsql") -> str:
    """Render the table's full identifier - catalog/db/schema/name, or a temp
    sigil (#x, ##x, @x) - without its alias. Reconstructing this manually
    from table.catalog/table.db/table.name silently drops the schema on a
    4-part linked-server reference (LinkedServer.Database.dbo.Table), because
    sqlglot nests "dbo.Table" as a single Dot expression under .this rather
    than exposing dbo as its own property. Cloning and stripping the alias
    then asking sqlglot to render it sidesteps that - it already knows how to
    print its own structure correctly, including cross-database references."""
    clone = table.copy()
    clone.set("alias", None)
    return clone.sql(dialect=dialect)


def temp_key(table: exp.Table):
    """Normalized identity for a temp table / table variable ('#name',
    '##name', '@name'), or None if this is an ordinary table."""
    ident = table.this
    if isinstance(ident, exp.Identifier):
        if ident.args.get("temporary"):
            return "#" + ident.this.lower()
        if ident.args.get("global_"):
            return "##" + ident.this.lower()
    elif isinstance(ident, exp.Parameter):
        var = getattr(ident, "this", None)
        name = getattr(var, "this", None)
        if name:
            return "@" + str(name).lower()
    return None


def normalize_sql(node: exp.Expression, dialect: str = "tsql") -> str:
    try:
        return node.sql(dialect=dialect, normalize=True, pretty=False).lower()
    except Exception:
        return node.sql(dialect=dialect, pretty=False).lower()


def iter_scope_columns(node: exp.Expression, top: bool = True):
    """Yield exp.Column nodes belonging to this SELECT's own scope - stop
    descending into nested SELECTs/subqueries, since those are separate
    scopes walked by their own recursive call. Without this, a correlated or
    IN-subquery's columns would get misattributed to the outer scope."""
    if isinstance(node, exp.Column):
        yield node
        return
    if isinstance(node, (exp.Select, exp.Subquery)) and not top:
        return
    for child in node.iter_expressions():
        yield from iter_scope_columns(child, top=False)


class GraphBuilder:
    def __init__(self):
        self.nodes = {}
        self.edges = []
        self.table_refs = []  # [(label, target_id)]
        self.column_usage = defaultdict(dict)  # node_id -> {lower_name: display_name}
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

    def record_columns(self, node_id, names):
        bucket = self.column_usage[node_id]
        for name in names:
            if name:
                bucket[name.lower()] = name


def get_sources(select_expr: exp.Expression):
    """Direct FROM + JOIN sources of a single SELECT (not recursing into subqueries)."""
    sources = []
    from_ = get_arg(select_expr, "from")
    if from_ is not None:
        sources.append(from_.this)
    for j in get_arg(select_expr, "joins") or []:
        sources.append(j.this)
    return sources


def collect_columns(gb: GraphBuilder, select_expr: exp.Select, alias_map: dict):
    """alias_map: lowercased alias -> node_id, for sources directly in this
    scope. Attributes each column reference to the node it's qualified with;
    an unqualified column is attributed to the sole source if there's only
    one (the common single-table-CTE case), otherwise left unresolved rather
    than guessed at."""
    single_target = next(iter(alias_map.values())) if len(alias_map) == 1 else None
    for col in iter_scope_columns(select_expr):
        qualifier = (col.table or "").lower()
        if qualifier and qualifier in alias_map:
            node_id = alias_map[qualifier]
        elif not qualifier and single_target:
            node_id = single_target
        else:
            continue
        gb.record_columns(node_id, [col.name])


def connect_source(gb: GraphBuilder, src, target_id, ctes_in_scope, dialect):
    """Wire src into target_id. Returns (node_id, alias) for column
    attribution, or None if src isn't a table/CTE/subquery."""
    if isinstance(src, exp.Table):
        key = src.name.lower()
        alias = src.alias_or_name
        if key in ctes_in_scope and not temp_key(src):
            node_id = ctes_in_scope[key]
            gb.add_edge(node_id, target_id)
        else:
            label = table_label(src, dialect)
            node_id = f"table:{label.lower()}"
            gb.add_node(node_id, type="table", label=label)
            gb.add_edge(node_id, target_id)
            gb.table_refs.append((label, target_id))
        return node_id, alias
    elif isinstance(src, exp.Subquery):
        inner = src.this
        alias = src.alias or gb.auto_alias()
        sub_id = f"subquery:{alias}_{id(src)}"
        gb.add_node(sub_id, type="subquery", label=f"({alias})", sql=normalize_sql(inner, dialect))
        gb.add_edge(sub_id, target_id)
        process_select(gb, inner, sub_id, ctes_in_scope, dialect)
        return sub_id, alias
    elif isinstance(src, (exp.Select, exp.Union)):
        connect_sources(gb, src, target_id, ctes_in_scope, dialect)
    # anything else (table-valued function, PIVOT, etc.) is left unconnected -
    # not a table/CTE/subquery source, so it doesn't belong in the lineage graph
    return None


def has_bare_star(select_expr: exp.Select) -> bool:
    for item in get_arg(select_expr, "expressions") or []:
        if isinstance(item, exp.Star):
            return True
        if isinstance(item, exp.Column) and item.name == "*" and not item.table:
            return True
    return False


def connect_sources(gb: GraphBuilder, select_or_union, target_id, ctes_in_scope, dialect):
    if isinstance(select_or_union, exp.Union):
        connect_sources(gb, select_or_union.left, target_id, ctes_in_scope, dialect)
        connect_sources(gb, select_or_union.right, target_id, ctes_in_scope, dialect)
        return
    if not isinstance(select_or_union, exp.Select):
        return

    alias_map = {}
    for src in get_sources(select_or_union):
        result = connect_source(gb, src, target_id, ctes_in_scope, dialect)
        if result:
            node_id, alias = result
            if alias:
                alias_map[alias.lower()] = node_id

    collect_columns(gb, select_or_union, alias_map)
    if has_bare_star(select_or_union) and target_id in gb.nodes:
        gb.nodes[target_id]["select_star"] = True


def process_select(gb: GraphBuilder, select_expr, node_id, ctes_in_scope, dialect):
    """Register this SELECT's own WITH clause (if any) as CTE nodes, then wire
    its FROM/JOIN sources into node_id."""
    local_ctes = dict(ctes_in_scope)
    target = select_expr
    with_ = get_arg(select_expr, "with")

    if with_:
        for cte in with_.expressions:
            alias = cte.alias
            cte_id = f"cte:{alias}"
            gb.add_node(cte_id, type="cte", label=alias, sql=normalize_sql(cte.this, dialect))
            local_ctes[alias.lower()] = cte_id
        for cte in with_.expressions:
            cte_id = local_ctes[cte.alias.lower()]
            connect_sources(gb, cte.this, cte_id, local_ctes, dialect)

    connect_sources(gb, target, node_id, local_ctes, dialect)


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


MAX_DUPLICATE_LOGIC_RESULTS = 25


def find_duplicate_logic(body_nodes, threshold=DUPLICATE_SIMILARITY_THRESHOLD):
    """body_nodes: list of (id, normalized_sql). Flags near-identical bodies -
    the concrete signal that two CTEs/subqueries should be consolidated into
    one. Returns (results, total_count): on a script with dozens of near-
    identical CTEs (usually a copy-pasted pattern repeated many times) the
    full pairwise list can run into the hundreds, so results are capped for
    display/scoring while total_count keeps the real count for context."""
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
    results.sort(key=lambda r: -r["similarity"])
    return results[:MAX_DUPLICATE_LOGIC_RESULTS], len(results)


# ---------------------------------------------------------------------------
# Sargability heuristics: static red flags that keep the query engine from
# using an index, even though we can't see the actual indexes or a real
# execution plan. Each finding is a hint worth a look, not a proven cost.
# ---------------------------------------------------------------------------

def _snippet(node: exp.Expression, dialect: str, limit: int = 90) -> str:
    text = node.sql(dialect=dialect)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _func_wraps_column(func: exp.Expression) -> bool:
    return any(isinstance(c, exp.Column) for c in func.iter_expressions())


def sargability_findings(main: exp.Expression, dialect: str):
    findings = []

    for where in main.find_all(exp.Where):
        for func in where.find_all((exp.Func, exp.Cast)):
            if _func_wraps_column(func):
                findings.append({
                    "type": "non_sargable_predicate",
                    "detail": f"Filter wraps a column in {type(func).__name__.upper()}, which usually blocks index use: {_snippet(func, dialect)}",
                })

    for join in main.find_all(exp.Join):
        on = join.args.get("on")
        if on is not None:
            if not list(on.find_all(exp.Column)):
                findings.append({
                    "type": "cartesian_join",
                    "detail": f"Join condition doesn't reference any columns (e.g. ON 1=1) - a disguised cartesian product: {_snippet(join, dialect)}",
                })
            for func in on.find_all((exp.Func, exp.Cast)):
                if _func_wraps_column(func):
                    findings.append({
                        "type": "non_sargable_predicate",
                        "detail": f"Join condition wraps a column in {type(func).__name__.upper()}, which usually blocks index use: {_snippet(func, dialect)}",
                    })
        else:
            kind = (join.args.get("kind") or "").upper()
            if kind != "CROSS" and not join.args.get("using"):
                findings.append({
                    "type": "cartesian_join",
                    "detail": f"Join has no ON/USING predicate (possible cartesian product): {_snippet(join, dialect)}",
                })

    for like in main.find_all(exp.Like):
        pattern = like.expression
        if isinstance(pattern, exp.Literal) and pattern.is_string and pattern.this.startswith("%"):
            findings.append({
                "type": "leading_wildcard_like",
                "detail": f"Leading-wildcard LIKE can't seek an index, forces a scan: {_snippet(like, dialect)}",
            })

    return findings


# ---------------------------------------------------------------------------
# Temp table / table variable indexing: scanned across the whole script (all
# batches), since a #temp table's CREATE INDEX and its later JOIN usage are
# routinely in different GO batches, and a temp table persists across
# batches within the same session.
# ---------------------------------------------------------------------------

def scan_temp_objects(all_stmts, dialect: str):
    created = {}
    indexed = set()
    join_usage = defaultdict(int)

    for stmt in all_stmts:
        for sel in stmt.find_all(exp.Select):
            into = get_arg(sel, "into")
            if into is not None and isinstance(into.this, exp.Table):
                key = temp_key(into.this)
                if key:
                    created.setdefault(key, {"label": table_label(into.this, dialect), "created_via": "SELECT ... INTO"})

        if isinstance(stmt, exp.Create) and (stmt.args.get("kind") or "").upper() == "TABLE":
            schema = stmt.this
            tbl = schema.this if isinstance(schema, exp.Schema) else schema
            if isinstance(tbl, exp.Table):
                key = temp_key(tbl)
                if key:
                    created.setdefault(key, {"label": table_label(tbl, dialect), "created_via": "CREATE TABLE"})

        if isinstance(stmt, exp.Create) and (stmt.args.get("kind") or "").upper() == "INDEX":
            idx = stmt.this
            tbl = idx.args.get("table") if isinstance(idx, exp.Index) else None
            if isinstance(tbl, exp.Table):
                key = temp_key(tbl)
                if key:
                    indexed.add(key)

        if isinstance(stmt, exp.Declare):
            for item in getattr(stmt, "expressions", []) or []:
                if isinstance(item.args.get("kind"), exp.Schema):
                    params = item.this if isinstance(item.this, list) else [item.this]
                    for p in params:
                        var = p.this if isinstance(p, exp.Parameter) else p
                        name = getattr(var, "this", None)
                        if name:
                            key = "@" + str(name).lower()
                            created.setdefault(key, {"label": "@" + str(name), "created_via": "table variable (DECLARE ... TABLE)"})

        for j in stmt.find_all(exp.Join):
            t = j.this
            if isinstance(t, exp.Table):
                key = temp_key(t)
                if key:
                    join_usage[key] += 1

        # a Join node only captures the *joined-in* side; the FROM table a
        # join is built on top of is just as much a join participant and
        # deserves the same scrutiny
        for sel in stmt.find_all(exp.Select):
            if get_arg(sel, "joins"):
                from_ = get_arg(sel, "from")
                t = from_.this if from_ is not None else None
                if isinstance(t, exp.Table):
                    key = temp_key(t)
                    if key:
                        join_usage[key] += 1

    findings = []
    for key, info in created.items():
        joins = join_usage.get(key, 0)
        has_index = key in indexed
        if joins > 0 and not has_index:
            note = (
                "Referenced in a JOIN but never indexed - if it holds more than a handful of "
                "rows, this forces a scan every time it's joined."
                if not key.startswith("@")
                else "Table variables can't easily be indexed after creation (and pre-2014 SQL "
                     "Server statistics on them are unreliable) - if this holds many rows, a "
                     "#temp table with an explicit index is usually faster."
            )
            findings.append({
                "object": info["label"],
                "created_via": info["created_via"],
                "has_index": has_index,
                "joined_times": joins,
                "note": note,
            })
    return findings


# ---------------------------------------------------------------------------
# Score: a static-analysis estimate, not a measured cost. Weighted toward the
# signals that actually predict slow queries (missing indexes, non-sargable
# predicates, cartesian joins) and DRY duplication; complexity is a light
# secondary factor, not the driver, per how this skill is meant to be used.
# ---------------------------------------------------------------------------

MAX_DUPLICATE_BREAKDOWN_ITEMS = 8


def compute_score(table_usage, duplicate_logic, duplicate_logic_total, sarg_findings, temp_findings, complexity):
    score = 100.0
    breakdown = []

    for dup in duplicate_logic[:MAX_DUPLICATE_BREAKDOWN_ITEMS]:
        impact = -8 if dup["exact"] else -5
        score += impact
        breakdown.append({
            "factor": "Duplicate CTE/subquery logic",
            "impact": impact,
            "detail": f"{dup['a'].split(':')[-1]} ↔ {dup['b'].split(':')[-1]} ({round(dup['similarity'] * 100)}% match)",
        })
    extra_dups = duplicate_logic_total - MAX_DUPLICATE_BREAKDOWN_ITEMS
    if extra_dups > 0:
        impact = -min(extra_dups * 3, 20)
        score += impact
        breakdown.append({
            "factor": "Duplicate CTE/subquery logic",
            "impact": impact,
            "detail": f"+{extra_dups} more near-duplicate pairs beyond the top {MAX_DUPLICATE_BREAKDOWN_ITEMS} shown - this script likely has a systemic copy-paste pattern worth a closer look",
        })

    for t in table_usage:
        if t["duplicate"]:
            impact = -3 * min(t["count"] - 1, 3)
            score += impact
            breakdown.append({
                "factor": "Table referenced more than once",
                "impact": impact,
                "detail": f"{t['table']} referenced {t['count']}× (could be a self-join - worth a glance, not automatically wrong)",
            })

    finding_weights = {
        "non_sargable_predicate": -8,
        "leading_wildcard_like": -5,
        "cartesian_join": -12,
    }
    for f in sarg_findings:
        impact = finding_weights.get(f["type"], -5)
        score += impact
        breakdown.append({"factor": f["type"].replace("_", " ").capitalize(), "impact": impact, "detail": f["detail"]})

    for f in temp_findings:
        impact = -10
        score += impact
        breakdown.append({
            "factor": "Unindexed temp object used in a join",
            "impact": impact,
            "detail": f"{f['object']} ({f['created_via']}): {f['note']}",
        })

    star_count = complexity.get("select_star_count", 0)
    if star_count:
        impact = -2 * min(star_count, 3)
        score += impact
        breakdown.append({
            "factor": "SELECT * used",
            "impact": impact,
            "detail": f"{star_count} SELECT * in this statement - pulls unneeded columns and hides what's actually used downstream",
        })

    # Complexity: intentionally light-weight, a secondary factor rather than the driver.
    join_count = complexity.get("join_count", 0)
    cte_count = complexity.get("cte_count", 0)
    max_layer = complexity.get("max_layer", 0)
    complexity_penalty = 0
    if join_count > 4:
        complexity_penalty += min(join_count - 4, 6)
    if cte_count > 5:
        complexity_penalty += min(cte_count - 5, 5)
    if max_layer > 4:
        complexity_penalty += min(max_layer - 4, 4)
    if complexity_penalty:
        impact = -complexity_penalty
        score += impact
        breakdown.append({
            "factor": "Structural complexity",
            "impact": impact,
            "detail": f"{join_count} joins, {cte_count} CTEs, {max_layer} lineage layers deep - complexity is a minor factor here, readability/maintainability call is yours",
        })

    score = max(0, min(100, round(score)))
    breakdown.sort(key=lambda b: b["impact"])
    return score, breakdown


def analyze(sql_text: str, dialect: str = "tsql", source_name: str = "query"):
    batches = split_batches(sql_text) or [sql_text]
    warnings = []
    statements_out = []
    global_body_nodes = []  # [(prefixed_id, normalized_sql)]
    global_table_refs = []  # [label]
    stmt_index = 0

    all_parsed_stmts = []
    per_batch_parsed = []
    for batch in batches:
        try:
            parsed = sqlglot.parse(batch, dialect=dialect)
        except Exception as e:
            warnings.append(f"Could not parse a batch: {clean_error(e)}")
            per_batch_parsed.append([])
            continue
        parsed = [s for s in parsed if s is not None]
        per_batch_parsed.append(parsed)
        all_parsed_stmts.extend(parsed)

    temp_object_findings = scan_temp_objects(all_parsed_stmts, dialect)
    temp_findings_by_key = {}
    for f in temp_object_findings:
        temp_findings_by_key[f["object"].lstrip("#@").lower()] = f

    for parsed in per_batch_parsed:
        for stmt in parsed:
            main = unwrap_statement(stmt)
            if main is None:
                continue  # e.g. plain DDL with no query body - nothing to map

            stmt_index += 1
            gb = GraphBuilder()
            output_id = "output"
            gb.add_node(output_id, type="output", label="Result")

            try:
                process_select(gb, main, output_id, {}, dialect)
            except Exception as e:
                warnings.append(f"Statement {stmt_index}: could not build graph ({clean_error(e)})")
                stmt_index -= 1
                continue

            layers = compute_layers(gb)
            for nid, layer in layers.items():
                gb.nodes[nid]["layer"] = layer

            for nid, cols in gb.column_usage.items():
                if nid in gb.nodes:
                    gb.nodes[nid]["columns"] = sorted(cols.values(), key=str.lower)

            usage_counter = Counter(label for label, _ in gb.table_refs)
            table_usage = [
                {"table": label, "count": count, "duplicate": count > 1}
                for label, count in sorted(usage_counter.items(), key=lambda x: (-x[1], x[0]))
            ]

            body_nodes = [(nid, n.get("sql")) for nid, n in gb.nodes.items() if n.get("sql")]
            duplicate_logic, duplicate_logic_total = find_duplicate_logic(body_nodes)

            try:
                sarg_findings = sargability_findings(main, dialect)
            except Exception as e:
                sarg_findings = []
                warnings.append(f"Statement {stmt_index}: sargability check skipped ({clean_error(e)})")

            stmt_temp_findings = [
                f for f in temp_object_findings
                if temp_key_matches_statement(f, main)
            ]

            select_star_count = sum(1 for n in gb.nodes.values() if n.get("select_star"))
            complexity = {
                "join_count": len(list(main.find_all(exp.Join))),
                "cte_count": sum(1 for n in gb.nodes.values() if n["type"] == "cte"),
                "max_layer": max((n.get("layer", 0) for n in gb.nodes.values()), default=0),
                "select_star_count": select_star_count,
            }
            score, breakdown = compute_score(
                table_usage, duplicate_logic, duplicate_logic_total, sarg_findings, stmt_temp_findings, complexity
            )

            statements_out.append({
                "index": stmt_index,
                "nodes": list(gb.nodes.values()),
                "edges": gb.edges,
                "table_usage": table_usage,
                "duplicate_logic": duplicate_logic,
                "duplicate_logic_total": duplicate_logic_total,
                "sargability_findings": sarg_findings,
                "temp_object_findings": stmt_temp_findings,
                "score": {"value": score, "breakdown": breakdown},
            })

            global_table_refs.extend(label for label, _ in gb.table_refs)
            global_body_nodes.extend((f"stmt{stmt_index}:{nid}", sql) for nid, sql in body_nodes)

    overall_usage_counter = Counter(global_table_refs)
    overall_table_usage = [
        {"table": label, "count": count, "duplicate": count > 1}
        for label, count in sorted(overall_usage_counter.items(), key=lambda x: (-x[1], x[0]))
    ]
    if stmt_index > 1:
        cross_statement_duplicate_logic, cross_statement_duplicate_logic_total = find_duplicate_logic(global_body_nodes)
    else:
        cross_statement_duplicate_logic, cross_statement_duplicate_logic_total = [], 0

    return {
        "source": source_name,
        "dialect": dialect,
        "statement_count": stmt_index,
        "warnings": warnings,
        "statements": statements_out,
        "overall_table_usage": overall_table_usage,
        "cross_statement_duplicate_logic": cross_statement_duplicate_logic,
        "cross_statement_duplicate_logic_total": cross_statement_duplicate_logic_total,
        "temp_object_findings": temp_object_findings,
    }


def temp_key_matches_statement(finding, main: exp.Expression) -> bool:
    """True if this statement actually reads the temp object as a FROM/JOIN
    source - not just mentions its name, which would also match the INTO
    clause of the statement that *creates* it and misattribute the finding
    there instead of to the statement that joins it unindexed."""
    label = finding["object"].lower().lstrip("#@")
    for sel in main.find_all(exp.Select):
        for src in get_sources(sel):
            if isinstance(src, exp.Table):
                key = temp_key(src)
                if key and key.lstrip("#@") == label:
                    return True
    return False


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
