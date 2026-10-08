# dbt_markers

A dbt package that appends a one-line JSON comment to every SQL statement dbt
runs, recording which invocation and which node issued it. It is a single
macro used through dbt's `query-comment` setting: it runs no SQL, adds no
hooks and issues no extra statements.

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

## Privacy

The comment is stored in your warehouse's query history, and anyone who can
read that history can read it. `node_meta`, `invocation_vars` and
`dbt_cloud_run_reason` are copied exactly as given, so do not put secrets or
personal data in `meta` or `--vars`; use `env_var()` for secrets instead.

## FAQ

**No comment shows up in query history.** Check that `append: true` is set
(Snowflake discards comments placed before the statement) and that `dbt deps`
has run. `SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY` can lag by up to 45 minutes.

**How do I turn it off?** Remove the `query-comment` block. To enable it for
one target only, use
`comment: "{{ dbt_markers.query_comment(node) if target.name == 'prod' else '' }}"`.

## License

MIT
