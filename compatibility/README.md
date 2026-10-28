# Compatibility verification

The package supports dbt `>=1.6.0,<3.0.0`. The sweep tests non-yanked stable
releases, not prereleases or every possible Python/adapter combination.

`run.py` installs the package into a disposable project and checks parsing,
seeding, model execution, cached parsing, builds, strict mode, disabled delivery,
and unit tests where available. It compares decoded comments with the emitted
manifest, includes unselected nodes beyond the former 16 KB chunk size, and verifies that
metadata statements precede selected model SQL. Core uses its real `dbtRunner`
entry point sequentially within one process; v2 uses its native CLI. Warehouse
execution in the local sweep uses DuckDB. Snowflake adapters are also installed
and resolved to versions compatible with each Core patch.

```sh
python3 compatibility/sweep.py --directory /tmp/markers-core --workers 7
python3 compatibility/sweep.py --engine v2 --directory /tmp/markers-v2 \
  --package-ref FULL_40_CHARACTER_COMMIT
```

The sweep needs `uv`, Python 3.11, and network access. Every case retains its
dependency freeze, console output, SQL events, artifacts, and results. Use a fresh
output directory for reruns. For one existing environment:

```sh
python3 compatibility/run.py --dbt /path/to/dbt --directory /tmp/markers-one \
  --unit-tests --package-ref FULL_40_CHARACTER_COMMIT
```

Add `--legacy --package-ref v1.0.0` instead to check the guarded-hook rollback.
The canary's local package path is for local tests only: the Cloud CLI does not
support relative local packages, so Cloud tests use a pinned Git dependency.

## Behavior differences

- Core 1.6–1.7 can send graphs for both `run` and `build`/`test`.
- Core 1.8–1.11 expose neither unit-test definitions nor their selected IDs in the
  runtime graph. `run` can send its graph, but `build`/`test` conservatively fall
  back in `auto` mode and fail in `required` mode, even if the project has no unit
  tests. This avoids claiming that missing definitions are known not to exist.
- Core 1.12 sends build/test graphs when the unit-test collection is empty;
  projects with unit tests fall back because selection omits their IDs.
- v2 exposes unit tests and includes ephemeral ancestors in selection. Its graph
  excludes hook-operation nodes that Core includes. Consumers should use resource
  types instead of assuming every node corresponds to a warehouse query.
- All modes retain the existing per-query marker format. Graph delivery runs once
  per invocation, as one separate metadata query.
- A missing graph or an explicit unavailable status is a per-query-only fallback,
  never permission to reuse a previous invocation's configuration.

## Cloud canary

The canary branch is `kuza/markers-manifest-canary` in
[`espressocomputing/dbt-tpch`](https://github.com/espressocomputing/dbt-tpch).
It pins the package by full Git SHA and selects only `markers_probe`, a view over
a constant ephemeral input. Other TPC-H models are parsed for graph coverage but
are not executed. It uses a dedicated transient database and schema.

Cloud verification must inspect remote run status and artifacts, not just the
CLI exit code: the Cloud CLI can return zero after a remote dbt operation fails.
The [checked-in results](results.json) record the original chunked transport sweep.
They are historical evidence for graph extraction and fallback behavior, not a
Cloud verification of transport version 2.
Cloud release tracks can change independently of local Core releases.

## Single-query transport verification

Transport version 2 passed the real dbt canary with Git revision
`c7376b830f345d3316fda7a486e6c9c5658ebd5c` on Core 1.6.0, 1.10.23,
1.12.6, and v2 2.0.9. Each graph delivery used exactly one metadata query before
model SQL; cached parsing, strict/off modes, and unit-test fallback checks passed.
Eight macro/decoder tests passed, including one statement containing a 6,001-node
payload larger than 1 MB. The large-payload test renders and decodes locally;
it does not establish a warehouse POST limit. Cloud was not rerun for version 2.

Core 1.6.0 used the earlier sweep environment with protobuf 4.25.9. A fresh
installation resolved protobuf 7.36.2 and failed during `dbt deps` with maximum
recursion depth exceeded, before the graph hook ran. Preserve compatible
transitive dependencies when running this older dbt release.

## Query text verification

Revision `ff22794849549b8d98f69302f34858c733df9d68` passed on Core 1.6.0,
1.10.23, 1.12.6, and v2 2.0.9, plus eight macro/decoder tests. All 116 model/test
nodes in each canary graph carried raw source matching the manifest, including
singular and generic tests. None carried nonempty compiled code at `on-run-start`.
Each graph still used one metadata query. Core 1.6 used the compatible environment
described above. Cloud was not rerun for the query-text change.
