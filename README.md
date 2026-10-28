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
`raw_code_md5` (the code itself is never included), `node_tags`, `node_meta`.

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
The hook emits one or more `select 1` statements with trailing JSON comments.
They travel through the configured warehouse connection, so an Espresso proxy
can receive them without an HTTP endpoint, new credentials, or dbt Cloud API.
These precede model execution, **not necessarily all SQL**: connection setup,
schema introspection, and earlier hooks may already have run. There is no shared
"first query" flag or graph attached to every model query.

```sql
select 1 /* {"dbt_markers_manifest": 1, "invocation_id": "...",
  "manifest_id": "<md5 of payload>", "chunk_index": 0, "chunk_count": 1,
  "data": "<JSON payload fragment>"} */
```

The payload uses `schema: dbt_markers.graph.v1` and contains invocation, project,
dbt version, command, target name, `available_collections`, `selected_resources`, and a `nodes` map keyed
by unique ID. Each node has its resource type and `depends_on` IDs, plus available
relation identifiers and materialization. It includes unselected nodes, sources,
ephemeral nodes, and the other resources exposed by dbt's runtime `graph` context.
Selection is kept separate from the project graph; ephemeral nodes are logical
dependencies, not necessarily separately executable work.

This is a **graph projection, not the full `manifest.json` artifact**. It omits
SQL, macros, docs, arbitrary config/meta, and disabled resources. Resources not
exposed by a dbt engine's `graph` context cannot be exported (for example, Core
1.10 does not expose the manifest's separate `unit_tests` collection there).
Core 1.8–1.11 also omit unit tests from `selected_resources`, so absence of missing
references alone cannot establish a complete execution graph for `build`/`test`.
The hook falls back when the engine cannot expose unit-test definitions for those
commands. Core 1.6–1.7 have no unit-test feature; Core 1.12 and v2 expose that collection.
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

Chunks carry at most 16,000 payload characters, numbered from zero. Concatenate
decoded `data` fields in index order and verify the UTF-8 payload's MD5 against
`manifest_id` before decoding the graph. MD5 detects transfer errors; it is not
authentication. The example rejects incomplete transfers, conflicting duplicates,
mixed invocations, and unresolved node references. Identical retries are accepted.

A production receiver must key assembly by authenticated customer plus invocation
and manifest IDs, bound memory and lifetime, and install configuration only after
the entire graph validates. It should recognize these as metadata statements,
not workload queries. Use the proxy request stream rather than delayed/truncated
warehouse query history. If statements reach Snowflake, they execute ordinary
`select 1` queries. Hook query failures abort the run; there is no separate receipt
acknowledgment from Nitro. No model-completion events or run results are sent.

## Privacy

The comment is stored in your warehouse's query history, and anyone who can
read that history can read it. `node_meta`, `invocation_vars` and
`dbt_cloud_run_reason` are copied exactly as given, so do not put secrets or
personal data in `meta` or `--vars`; use `env_var()` for secrets instead.
Opt-in graph comments also reveal project structure and relation names. The graph
projection does not add model SQL or arbitrary metadata; the existing query-comment
settings still apply to metadata queries too.

## FAQ

**No comment shows up in query history.** Check that `append: true` is set
(Snowflake discards comments placed before the statement) and that `dbt deps`
has run. `SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY` can lag by up to 45 minutes.

**How do I turn it off?** Remove the `query-comment` block. To enable it for
one target only, use
`comment: "{{ dbt_markers.query_comment(node) if target.name == 'prod' else '' }}"`.

## License

MIT
