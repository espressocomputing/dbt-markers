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
`target_name`, `target_database`, `target_schema`. The optional Core runner
integration also emits structured `invocation_args` on every marked statement.

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

## Programmatic Core invocations

Core builds `flags.INVOCATION_COMMAND` from the Python process's `sys.argv`,
not the arguments passed to `dbtRunner.invoke()`. In a worker process this can
produce just `"dbt"` or capture unrelated worker arguments. Core's query-comment
context also omits `invocation_args_dict` and the selection flags. Ordinary
markers cannot recover that information from a model's name or directory.

After `dbt deps`, replace the runner import in the application launching Core:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path("dbt_packages/dbt_markers").resolve()))
from dbt_markers_runner import dbtRunner

result = dbtRunner().invoke(["build", "--selector", "nexus-core"])
```

Resolve the package path relative to your dbt project if the worker runs elsewhere.
Keep the existing `query-comment` configuration. No job label or hook is needed.
The bridge temporarily extends Core's Jinja flags with an allowlist of its actual
parsed arguments, including keyword overrides and dbt defaults. It restores Core's
original getter when `invoke()` returns. Like Core's runner, this integration uses
process-global flags: run concurrent invocations in separate processes.
This Python integration is for Core, not Fusion or the dbt Cloud CLI.

Every marked statement, including setup queries without a node, then carries:

```json
"invocation_args": {
  "which": "build",
  "project_dir": "/worker/project",
  "select": [],
  "exclude": [],
  "selector": "nexus-core",
  "vars": {},
  "full_refresh": false
}
```

Available fields also include `profile`, `target`, `defer`, `state`, `defer_state`,
`favor_state`, `indirect_selection`, `resource_types`, `exclude_resource_types`,
and `empty`. Values retain their JSON types; fields absent in a Core version are
omitted. `profile` and `target` can be null when defaulted; the resolved target
is still recorded in `target_name`. `invocation_command` reflects the arguments passed to `invoke()`;
`invocation_args` is authoritative for keyword overrides. `invocation_vars` is
JSON-serialized from the parsed vars when using the bridge.

Consumers can group each query by project, target, command, and selection without
looking up its invocation or observing another model. Keep the fields themselves:
this package does not prescribe a hash or job identity. Absolute checkout/state
paths and date-valued vars can change between runs; two schedules with identical
arguments still describe the same workload. Without the runner integration,
`invocation_args` is omitted rather than claiming an empty selection.

Reproduce the missing command and verify the bridge against a disposable DuckDB
project using an environment with Core and dbt-duckdb installed:

```sh
python compatibility/invocation.py --directory /tmp/markers-invocation
```

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

## Privacy

The comment is stored in your warehouse's query history, and anyone who can
read that history can read it. `node_meta`, `invocation_vars`, `invocation_args.vars` and
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
