---
name: sql-source-mapper
description: Maps out where a SQL query's data comes from and renders it as an interactive diagram — source tables flowing through CTEs/subqueries/joins to the final result — with a searchable column-level breakdown per source, DRY-duplicate warnings (tables referenced more than once, near-identical CTE/subquery logic), a heuristic 0-100 performance score (indexing/sargability red flags, unindexed temp tables, DRY, complexity), and a shared team library so results are browsable across scripts, not just one at a time. Built and tested for T-SQL (SQL Server / Azure SQL), works with other sqlglot-supported dialects too. Use this whenever someone pastes or points to a SQL query/script and asks to visualize it, map or trace its sources/lineage, see what tables or columns it touches, spot duplicate tables or CTEs, check for repeated logic, "DRY up" their SQL, score or rate a query's performance, check for missing indexes (including on temp tables), or wants a shared/browsable place for the team's analyzed queries — even without those exact words, e.g. "what tables does this query actually hit", "am I duplicating anything in this script", "draw out where this data comes from", "does this query join the same table twice", "what columns come from this table", "is this temp table indexed", "rate how bad this query is", "add this to our team's query library".
---

# SQL Source Mapper

Turns a SQL query or script into a visual lineage map, a concrete list of
what's duplicated, a heuristic performance score, and (optionally) an entry
in a shared library the whole team can browse — so the user can see their
sources at a glance, keep their SQL DRY, and catch the kind of thing that
only shows up once a script has grown to thousands of lines and dozens of
objects (a table joined twice three CTEs apart, a temp table nobody indexed,
the same lookup logic three different people wrote separately).

Don't hand-roll SQL parsing with regex — table names, CTE boundaries, and
subquery nesting are exactly the kind of thing that looks simple until a
comment, a bracketed identifier, or a nested `WITH` breaks the pattern. This
skill bundles a real parser (`sqlglot`, dialect `tsql` by default) that builds
an actual AST, so the lineage graph is precise even for gnarly queries.

## Workflow

1. **Get the SQL into a file.** If the user pasted a query in chat, write it
   to a `.sql` file in the scratchpad directory. If they pointed at a file,
   use it directly. Multi-statement scripts (including ones with `GO` batch
   separators, and temp tables/table variables that carry across batches)
   are fine — the analyzer handles them.

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
   mapped instead of presenting the diagram/score as if they covered everything.

4. **Build the diagram:**
   ```
   python3 <skill-path>/scripts/build_diagram.py <out>.json -o <out>.html
   ```
   This produces one self-contained HTML file (no external requests, works
   offline, adapts to light/dark theme) with:
   - a layered graph — base tables on the left, flowing through CTEs and
     derived subqueries, into the final result on the right; draggable nodes,
     click-to-highlight (dims everything that isn't a direct neighbor)
   - a **search box** above the diagram — type a table/CTE/subquery name to
     jump straight to it, which matters once a script has dozens of objects
     and scrolling to find one gets impractical
   - a **Score** panel: a 0-100 estimate with an itemized breakdown (see below)
   - a **Tables used** panel, sorted by reference count, anything referenced
     more than once called out
   - a **Possible DRY duplicates** panel listing CTE/subquery pairs whose
     bodies are near-identical (≥90% text similarity)
   - an **Index & performance flags** panel (non-sargable predicates,
     cartesian joins, unindexed temp tables)
   - clicking any node shows the exact columns pulled from it downstream
     (not just "this table is used" — which specific fields), in the
     **Selected** panel

5. **Say what you found in words, too** — don't make the diagram the only
   place the answer lives. Lead with the score and the one or two things
   driving it down, then name any table referenced multiple times and any
   near-duplicate CTE/subquery pair with a one-line suggestion (e.g.
   "`dup_check` and `active_customers` are functionally the same — worth
   collapsing into one CTE"). A repeated table reference isn't automatically
   wrong (self-joins are legitimate SQL), so frame it as something to glance
   at, not an error.

6. **Offer to publish it to the shared library** (see below) if the user is
   working in a team repo — a one-off diagram answers "what does this query
   do," but the library is what actually lets the DRY question span more
   than one script.

## The score: what it means and doesn't

`score.value` (0-100) and `score.breakdown` (itemized +/- reasons) come from
static analysis of the SQL text only — there's no live database connection,
no real execution plan, no actual index list. It's weighted toward what
usually matters for real performance (non-sargable predicates, cartesian
joins, unindexed temp tables, DRY duplication), with structural complexity
(join/CTE count, nesting depth) as a light secondary factor, not the driver —
a long, well-organized query with real indexes upstream can easily outscore
a short sloppy one. Always tell the user this is a heuristic estimate to
prioritize where to look, not a measured cost — don't state it as fact
("this query is slow"), and if they can supply real index metadata (e.g. a
`sys.indexes`/`sys.index_columns` export) at some point, that would let a
future version check sargable predicates against actual indexes instead of
guessing from the SQL text alone; there's no built-in way to feed that in yet.

## Reading the output JSON

`analyze_sql.py` emits one object per statement in the script (`statements`),
each with:
- `nodes`/`edges` — the lineage graph; each node carries `columns` (the
  distinct columns referenced from it downstream — this is what answers
  "what columns come from this source")
- `table_usage` — counts + a `duplicate` flag per base table
- `duplicate_logic` (capped at 25, with `duplicate_logic_total` alongside in
  case a script has a systemic copy-paste pattern producing far more pairs
  than are worth listing individually) — near-identical CTE/subquery bodies
- `sargability_findings` — non-sargable predicates, leading-wildcard `LIKE`,
  cartesian/predicate-less joins
- `temp_object_findings` — `#temp` tables / table variables that get
  joined/filtered on but never indexed (also mirrored at the top level,
  since a temp table's `CREATE INDEX` and its later join are often in
  different `GO` batches)
- `score` — see above

If the script has more than one statement, the top level also has
`overall_table_usage` and `cross_statement_duplicate_logic` — the same
signals computed across the whole file, for a table or a block of logic
copy-pasted between two queries in the same script.

## The shared library

A single diagram answers "what does this query do." The library is what
answers "has someone on the team already built this" — the actual DRY
question the user cares about, extended past one script.

```
python3 <skill-path>/scripts/publish_to_library.py \
  --analysis <out>.json --diagram <out>.html \
  --title "<short human name>" --author "<if known>" --notes "<one line, optional>"
```

Run this **from inside the team's own repo** (wherever their SQL scripts
already live), not from inside this skill's directory — pass `--library-dir`
if it isn't the default `./library`. It copies the analysis + diagram into
`library/<slug>/`, appends to `library/entries.json` (the manifest), and
regenerates `library/index.html`: a searchable page listing every analyzed
query (title, score, table/DRY flags) plus a **cross-script table index** —
which tables show up in more than one team member's script, the concrete
signal that two people independently built overlapping logic. Since it's all
just files in their repo, the team gets it by committing/pushing/pulling like
anything else — no separate service to stand up.

If it's not obvious where the team's shared repo is (this session might be
running somewhere else entirely), ask rather than guessing a location, and
don't invent an author name — leave `--author` off if you don't know it.

## Checking for duplication across separate files without the library

If someone wants a quick one-off comparison between two `.sql` files without
setting up the library, run the analyzer on each and diff the resulting
`table_usage` lists and CTE/subquery bodies yourself — say that's what you're
doing rather than silently only covering one file.
