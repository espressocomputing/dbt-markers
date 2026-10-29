# dbt_markers

A dbt package that appends a one-line JSON comment to every SQL statement dbt
runs, recording which invocation and which node issued it. It is a single
macro used through dbt's `query-comment` setting: by default it runs no SQL,
adds no hooks and issues no extra statements. Optional graph delivery uses an
`on-run-start` hook, described below.

```sql
select ... from analytics.stg_orders
/* {"dbt_markers": "1.0.0", "invocation_id": "0b1f…", "which": "build", …,
    "node_unique_id": "model.jaffle_shop.orders", "node_ref_names": ["stg_orders", "stg_payments"], …} */
```

## Install

Supports Snowflake, on dbt-core 1.6 or later and on dbt Fusion.

1. Add to `packages.yml`:

   ```yaml
   packages:
     - git: "https://github.com/espressocomputing/dbt-markers.git"
       revision: v1.0.0
   ```

2. Add to `dbt_project.yml`, replacing any existing `query-comment` block:

   ```yaml
   query-comment:
     comment: "{{ dbt_markers.query_comment(node) }}"
     append: true
   ```

3. Run `dbt deps`.

On dbt Cloud the steps are the same. To add your own keys, pass them in
`extra`, e.g. `dbt_markers.query_comment(node, extra={'owner': 'analytics'})`.
These can only add keys and never override the package's own.

## Keys

String keys are written only when they are non-empty. Key order is not
significant.

**Every statement:** `dbt_markers` (package version), `invocation_id`,
`which`, `invocation_command`, `invocation_vars`, `full_refresh`,
`run_started_at` (UTC, ISO 8601), `dbt_version`, `project_name`,
`target_name`, `target_database`, `target_schema`.

**Statements that belong to a node:** `node_unique_id`, `node_resource_type`,
`node_package_name`, `node_name`, `node_alias`, `node_original_file_path`,
`node_database`, `node_schema`, `materialized`, `incremental_strategy`,
`snowflake_warehouse`, `node_ref_names`, `node_depends_on`, `node_checksum`,
`raw_code_md5` (per-query markers omit the code), `node_tags`, `node_meta`.

**dbt Cloud runs:** each `DBT_CLOUD_*` run variable that is set (account,
project, environment id, name and type, job, run, run reason and category,
invocation context, PR id, git SHA and branch), with the name lower-cased.

`invocation_command` always starts with `dbt` and is collapsed onto one line.
The `--vars` value is shown as `<redacted>` there and is recorded word for
word in `invocation_vars`.

## Deliver the graph before model execution

Keep the existing `query-comment` configuration and add this hook first in your
root project's `dbt_project.yml`:

```yaml
on-run-start:
  - "{{ dbt_markers.emit_manifest() if dbt_markers.emit_manifest is defined else '' }}"
```

Pin a full commit containing `emit_manifest` in `packages.yml` and rerun
`dbt deps`. The `v1.0.0` tag predates graph delivery; the guarded hook above lets
you roll back to it and retain ordinary markers. Short commit hashes are not
portable across engines; use all 40 characters or a published tag.
This is opt-in; installing the package alone sends no graph.
The hook emits one `select 1` statement containing the complete graph comment.
It travels through the configured warehouse connection, so an Espresso proxy
can receive it without an HTTP endpoint, new credentials, or dbt Cloud API.
This precedes model execution, **not necessarily all SQL**: connection setup,
schema introspection, and earlier hooks may already have run. There is no shared
"first query" flag or graph attached to every model query.

```sql
select 1 /* {"dbt_markers_manifest": 2, "invocation_id": "...",
  "manifest_id": "<md5 of payload>",
  "data": "<complete JSON payload>"} */
```

The payload uses `schema: dbt_markers.graph.v1` and contains invocation, project,
dbt version, command, target name, `available_collections`, `selected_resources`, and a `nodes` map keyed
by unique ID. Each node has its resource type and `depends_on` IDs, plus available
relation identifiers and materialization. It includes unselected nodes, sources,
ephemeral nodes, and the other resources exposed by dbt's runtime `graph` context.
Nodes include `raw_code` (source SQL/Jinja, or Python for Python models),
`language`, and `compiled_code` when those fields are available. Model and test
source text is sent even for unselected nodes. Generic test source can be a Jinja
macro call rather than SQL. At `on-run-start`, dbt has generally not compiled
models and tests yet, so **compiled SQL is not guaranteed**. These fields are not
the final materialization statements sent to the warehouse. Nitro must receive
executed SQL from the normal query stream; this hook cannot provide all rendered
queries in advance.

Selection is kept separate from the project graph; ephemeral nodes are logical
dependencies, not necessarily separately executable work.

This is a **graph projection, not the full `manifest.json` artifact**. It omits
macros, docs, arbitrary config/meta, and disabled resources. Resources not
exposed by a dbt engine's `graph` context cannot be exported (for example, Core
1.10 does not expose the manifest's separate `unit_tests` collection there).
Core 1.8–1.11 also omit unit tests from `selected_resources`, so absence of missing
references alone cannot establish a complete execution graph for `build`/`test`.
The hook falls back when the engine cannot expose unit-test definitions for those
commands. Core 1.6–1.7 have no unit-test feature. Core 1.12 exposes definitions but
omits selected unit-test IDs, so projects with unit tests also fall back for
`build`/`test`. v2 exposes both definitions and selection.
Selection reflects the engine: v2 includes ephemeral ancestors that Core omits.

Control delivery with `vars: {dbt_markers_manifest_mode: auto}`:

| Mode | Behavior |
| --- | --- |
| `auto` (default) | Send the graph when supported. Otherwise log the reason and send a `dbt_markers_manifest_status: 1` comment with `status: unavailable`. Keep ordinary query markers. |
| `required` | Fail before model execution if the graph cannot be delivered completely. |
| `off` | Send no graph or status queries; keep ordinary query markers. |

Fallback never sends a partial graph. It does not catch database errors: failed
metadata SQL still fails the run. The receiver must not reuse an older invocation's
graph when the new invocation is unavailable or absent.
See [compatibility verification](compatibility/README.md) for tested versions,
packaging, and Cloud evidence. The package requires dbt 1.6 or later; fallback does
not bypass that installation constraint.

### Receiver / Nitro configuration demo

`examples/manifest_transport.py` is a reference decoder. Give it captured SQL
for one invocation:

```sh
python3 examples/manifest_transport.py captured.sql > nitro-graph.json
```

It produces `{"dbt_manifest": {...}}`, a **proposed configuration fragment**, not
an existing Nitro CLI contract. The binary integration still needs its actual
configuration interface. For a local transport demo using an existing Jinja2
environment (no warehouse needed):

```sh
python3 -m unittest discover -s tests -p '*_test.py'
python3 - <<'PY' > captured.sql
import runpy
render = runpy.run_path('tests/manifest_test.py')['render']
print('\n'.join(render(model_count=250)))
PY
python3 examples/manifest_transport.py captured.sql > nitro-graph.json
```

Transport version 2 sends the complete JSON payload in `data`, without chunking
or an application-level size cap. Verify its UTF-8 MD5 against `manifest_id`
before decoding the graph. MD5 detects transfer errors; it is not authentication.
The example rejects corrupt payloads, conflicting duplicates, mixed invocations,
and unresolved node references. Identical retries are accepted. The earlier
chunked version 1 is not accepted by this decoder.

A production receiver must associate configuration with the authenticated customer
and invocation, and install it only after the entire graph validates. Read the
complete comment directly from the request. Recognize the statement as metadata,
not workload queries. Use the proxy request stream rather than delayed/truncated
warehouse query history. If statements reach Snowflake, they execute ordinary
`select 1` queries. Hook query failures abort the run; there is no separate receipt
acknowledgment from Nitro. No model-completion events or run results are sent.

## Compiled planning prototype (unmerged)

`examples/plan.py` writes `dbt_markers.plan.v3` under `dbt_plan` for Nitro's
future configuration interface. Nodes contain **full compiled SQL**, source,
configuration, relation identity, declared and SQL-derived dependencies, and
SHA-256 fingerprints. Optional Core capture adds full materialization/hook SQL
and seed bindings in `generated_statements`; hashes never replace query text.

The planner's Python environment needs SQLGlot 30.x (verified with 30.13.0).
Use an installed dbt CLI with project dependencies already installed:

```sh
python examples/plan.py --dbt /path/to/dbt \
  --project-dir /path/to/project --profiles-dir /path/to/profiles \
  --target dev --warehouse-context account/warehouse/role \
  --command build --select orders --output plan.json
```

Add `--previous plan.json --output next-plan.json` to compare snapshots. Every
invocation runs `dbt ls` for selection and `dbt compile` for the **whole project**
with fresh artifacts. Compilation can query the warehouse through project macros.
Output paths must be new. Pass execution's target, vars and full-refresh settings;
warehouse context must identify account, warehouse and role without credentials.
Use `--default-database` and `--default-schema` only when they match the actual
session's resolution of unqualified names; otherwise these reads stay unresolved.

SQL lineage excludes CTE aliases, resolves physical relations to project nodes,
and creates explicit external/prior-state inputs. Ambiguous relations, opaque
SQL, unknown function bodies and cycles remain unresolved. `execution_edges`
separately represents data dependencies, build test gates, unit-test ordering,
and pre/post/project hooks. Ephemeral models remain logical nodes; they need not
correspond to separate warehouse queries.

`graph_hash` binds node fingerprints and compilation context. `plan_hash` also
binds selection and execution edges. Without capture, selection does not affect
the graph hash; capture enriches the selected nodes. Fingerprints include exact
SQL, config, dependencies and macro bodies. Metadata snapshots and parser versions
also participate in invalidation. `changes` propagates changes through both old
and new data/execution edges, including deleted nodes and tests. This is a
conservative invalidation set, **not permission to skip execution**.

### Stable comparison and coverage

The default comparison retains every literal. For explicitly designated runtime
output columns, add:

```sh
  --runtime-parameter-column dbt_batch_id=invocation_id \
  --runtime-parameter-column dbt_batch_ts=run_started_at
```

This is a caller-declared parameter contract, not automatic proof of a literal's
origin. Only string literals (optionally cast) directly assigned to those aliases
are parameterized; expressions are rejected. Other occurrences of the same value,
filters and business literals remain significant. Exact SQL and values remain in
the plan. `fingerprint` verifies the exact node; `comparison_fingerprint` and graph
hashes use the parameterized comparison. `runtime_parameters` records source spans
and values for compiled bodies. These templates are comparison representations,
not SQL ready to execute. Nitro must supply the new run's values when substituting
execution. A plan-v2 baseline cannot be compared with plan v3.

`coverage.compiled_bodies` lists available/missing SQL bodies, while
`coverage.materialization_sql` separates conditionally generated work from work
not captured. Unresolved selected dependencies remain explicit. Test SQL generation
does not prove a passing test, and all plans require runtime validation.

### Offline materialization capture

Add these options using Python from the same dbt Core environment:

```sh
  --capture-python /path/to/dbt-env/bin/python \
  --warehouse-state warehouse-state.json
```

That environment also needs SQLGlot (verified with 30.25.0). Capture is verified
on Core 1.12.6 with Snowflake 1.12.4 and DuckDB 1.11.1. The ordinary compiled
planner also has Core 1.6 and v2 smoke coverage; the internal Core capture hooks
are **not a stable cross-version plugin API**. Native v2 and remote dbt Cloud
capture are not supported or verified. Existing marker packaging is unchanged.

Capture runs dbt's real materialization macros with standard adapter connection
opening disabled. Relation/schema/column introspection uses an explicit snapshot:

```json
{
  "schema": "dbt_markers.warehouse_state.v1",
  "adapter_type": "snowflake",
  "databases": {
    "DATABASE": {"schemas": {"SCHEMA": {"relations": {
      "ORDERS": {"type": "table", "columns": [
        {"name": "ID", "data_type": "NUMBER"}
      ]}
    }}}}
  }
}
```

Use actual catalog identifier case. A listed schema's relation map must be
complete, including an empty map only when the schema really is empty. Missing
metadata blocks capture. For newly generated relations, `derived_columns` maps
the SHA-256 of the exact captured CREATE SQL to the column list; capture does not
invent expression types. Optional `query_results` maps exact stripped SQL to
`{"columns": [...], "rows": [[...]], "kind": "metadata"}`. Only use that kind for
metadata queries; data results default to `data` and retain their lineage.
`examples/snowflake_catalog.py` collects live standard table/view metadata using
Snowflake's connector. Run it with the same user/role as dbt:

```sh
python examples/snowflake_catalog.py --schemas schemas.json --output warehouse-state.json
```

`schemas.json` maps actual, case-sensitive database names to schema-name arrays.
`SNOWFLAKE_CONNECTION_JSON` contains connector options (or choose another variable
with `--connection-env`); no credentials are written to the snapshot. It reads
schema existence, relation types/transience, and exact column types. Uncollected
schemas have `relations: null`, which blocks capture rather than pretending they
are empty. Unsupported object types and potentially truncated SHOW results fail.
Snapshots include connection identity and collection time; collection time alone
does not invalidate the plan. Collection is not an atomic catalog snapshot, and
catalog/input state still needs validation before execution.

Unknown results are lazy: discarded results allow generation to continue, but
reading rows, column metadata or response values stops with an explicit blocker.
Provably empty projections and simple literal queries can be resolved locally.
A unit test can have all its SQL captured while its outcome remains unknown;
blockers distinguish SQL generation from result validation. Tests do not receive
invented passing outcomes, so dependent work can remain uncaptured.

`sql_generation_complete` marks individual generated materializations;
`selected_sql_generation_complete` requires successful capture of the selected
invocation. Both are conditional on current supplied metadata and statements
succeeding when executed. `execution_capture` retains the full trace, including
unattributed setup statements and blockers. It records adapter submissions, not
wire-level driver splitting, and omits query comments. Some adapter metadata
methods are replaced by snapshot lookups rather than captured SQL.

Capture is for trusted projects using the standard adapters. It is not a security
sandbox for arbitrary Python/custom plugins that open their own connections.
Python models, hidden stored-procedure/UDF dependencies, runtime-dependent hook
branches, missing metadata and results remain explicit limits. The preceding
whole-project compile is a normal dbt compile, not offline capture.

`--require-complete` rejects unresolved selected compilation/lineage; it does not
certify a complete wire trace. `execution_sql_complete`, `input_state_verified`
and `execution_reuse_authorized` stay false. Nitro must check inputs and actual
runtime SQL before reuse. This prototype writes JSON; it does not build the fused
megaquery, transmit a plan through the marker hook, or integrate with a Nitro
consumer. Keep this PR unmerged pending those acceptance requirements.

## Privacy

The comment is stored in your warehouse's query history, and anyone who can
read that history can read it. `node_meta`, `invocation_vars` and
`dbt_cloud_run_reason` are copied exactly as given, so do not put secrets or
personal data in `meta` or `--vars`; use `env_var()` for secrets instead.
Opt-in graph comments also reveal project structure and relation names. The graph
projection includes model and test source text, which can contain SQL literals.
It omits arbitrary metadata. Existing query-comment settings still apply to
metadata queries too.

## FAQ

**No comment shows up in query history.** Check that `append: true` is set
(Snowflake discards comments placed before the statement) and that `dbt deps`
has run. `SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY` can lag by up to 45 minutes.

**How do I turn it off?** Remove the `query-comment` block. To enable it for
one target only, use
`comment: "{{ dbt_markers.query_comment(node) if target.name == 'prod' else '' }}"`.

## License

MIT
