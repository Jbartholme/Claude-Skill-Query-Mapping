---
name: sql-source-mapper
description: Maps out where a SQL query's data comes from and renders it as an interactive diagram — source tables flowing through CTEs/subqueries/joins to the final result — plus a table-usage list and DRY-duplicate warnings that flag tables referenced more than once or near-identical CTE/subquery logic. Built and tested for T-SQL (SQL Server / Azure SQL), works with other sqlglot-supported dialects too. Use this whenever someone pastes or points to a SQL query/script and asks to visualize it, map or trace its sources/lineage, see what tables it touches, spot duplicate tables or CTEs, check for repeated logic, or "DRY up" their SQL — even without the word "diagram", e.g. "what tables does this query actually hit", "am I duplicating anything in this script", "draw out where this data comes from", "does this query join the same table twice".
---

# SQL Source Mapper

Turns a SQL query or script into a visual lineage map plus a concrete list of
what's duplicated, so the user can see their sources at a glance and keep
their SQL DRY (no copy-pasted subquery logic, no table pulled in twice when
one CTE would do).

Don't hand-roll SQL parsing with regex — table names, CTE boundaries, and
subquery nesting are exactly the kind of thing that looks simple until a
comment, a bracketed identifier, or a nested `WITH` breaks the pattern. This
skill bundles a real parser (`sqlglot`, dialect `tsql` by default) that builds
an actual AST, so the lineage graph is precise even for gnarly queries.

## Workflow

1. **Get the SQL into a file.** If the user pasted a query in chat, write it
   to a `.sql` file in the scratchpad directory. If they pointed at a file,
   use it directly. Multi-statement scripts (including ones with `GO` batch
   separators) are fine — the analyzer handles them.

2. **Analyze it:**
   ```
   python3 <skill-path>/scripts/analyze_sql.py <file>.sql --dialect tsql -o <out>.json
   ```
   `sqlglot` needs to be installed (`pip install sqlglot`) — check first if
   you're not sure it's available. If the script is written in a different
   dialect (Snowflake, BigQuery, Postgres, ...), pass the matching
   `--dialect` — see `sqlglot`'s dialect list if the user names one you're
   unsure about.

3. **Check `warnings` in the output JSON before anything else.** A parser
   can't fully understand every T-SQL construct (dynamic SQL built as a
   string, some newer Azure SQL syntax, `PIVOT`/`OPENJSON` edge cases), and
   when a batch fails to parse it's simply skipped rather than guessed at.
   If `warnings` is non-empty, tell the user plainly which part couldn't be
   mapped instead of presenting the diagram as if it covered everything.

4. **Build the diagram:**
   ```
   python3 <skill-path>/scripts/build_diagram.py <out>.json -o <out>.html
   ```
   This produces one self-contained HTML file (no external requests, works
   offline, adapts to light/dark theme) with:
   - a layered graph — base tables on the left, flowing through CTEs and
     derived subqueries, into the final result on the right
   - draggable nodes and click-to-highlight (clicking a node dims everything
     that isn't a direct neighbor, and shows its SQL body)
   - a **Tables used** panel, sorted by reference count, with anything
     referenced more than once called out
   - a **Possible DRY duplicates** panel listing CTE/subquery pairs whose
     bodies are near-identical (≥90% text similarity) — click one to
     highlight both nodes in the graph

5. **Publish it.** If the Artifact tool is available, publish the generated
   HTML file directly with it (the file is already a complete, self-contained
   page — no need to re-author it). Otherwise save the HTML file somewhere
   the user can find it and say so, or send it directly if a file-delivery
   tool is available.

6. **Say what you found in words, too** — don't make the diagram the only
   place the answer lives. Name any table referenced multiple times, and any
   near-duplicate CTE/subquery pair, with a one-line suggestion (e.g. "`dup_check`
   and `active_customers` are functionally the same — worth collapsing into
   one CTE"). A repeated table reference isn't automatically wrong (self-joins
   are legitimate SQL), so frame it as something to glance at, not an error.

## Reading the output JSON

`analyze_sql.py` emits one object per statement in the script (`statements`),
each with `nodes`/`edges` (the graph), `table_usage` (counts + a `duplicate`
flag), and `duplicate_logic` (near-identical CTE/subquery pairs with a
similarity score). If the script has more than one statement, it also emits
`overall_table_usage` and `cross_statement_duplicate_logic` — the same
signals computed across the whole file, useful for catching a table or a
block of logic that's been copy-pasted between two queries in the same
script.

## Checking for duplication across separate files

The DRY-duplicate detection works within one script (including across
multiple statements in that file). If someone wants to compare two *separate*
`.sql` files for overlap, run the analyzer on each and diff the resulting
`table_usage` lists and CTE/subquery bodies yourself — there's no built-in
cross-file mode, so say so if that's what they're after rather than silently
only covering one file.
