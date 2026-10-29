## Current capture contract

[Current export verification](sql-capture-results.json) records the capture-only
contract checks, separately from historical planning evidence.

The client now exports `dbt_markers.sql_capture.v1` (`dbt_capture`): original
manifests/run results, exact SQL and bindings, selected IDs, invocation context,
catalog inputs and explicit generation gaps. Comparison, hashes, parameter
normalization, inferred physical lineage and execution gates were removed.
The sections below record historical prototype verification; their plan/hash
assertions are not the current interface. SQL-generation comparisons remain
useful evidence.

`compatibility/planning.py` retains its filename but now tests artifact export,
exact compiled strings, declared dependencies, selected-component recovery,
vars, selection, incremental bodies and hook preservation. It no longer tests
server-side planning. `compatibility/capture.py` still exercises real Core adapter
capture; explicit derived-column fixtures now use exact CREATE SQL keys.

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

## Compiled planner prototype

Run `python3 compatibility/planning.py --dbt /path/to/dbt --directory /new/path`.
The suite uses real dbt compilation and warehouse fixtures, without mocking dbt.
Core 1.12.6 passed 17 plan scenarios: stable repeat, model/macro/variable edits,
selection changes, ephemeral expansion, relation changes, test edits/removal,
rewiring/deletion, incremental and hook handling, unit-test handling, warehouse
identity changes, and input-data changes. Strict unresolved plans, empty selection,
and corrupted prior plans were rejected without writing output. Core 1.6.0 (with
the previously documented compatible dependencies) and v2 2.0.9 passed baseline
and repeat smoke checks. The eight existing marker/decoder tests also passed.

The plans contain full compiled model/test query bodies as well as fingerprints.
These checks do not establish exact materialization SQL, input freshness, Cloud
compatibility, or a working Nitro integration. The planner remains a draft.

### Plan v2 and offline SQL capture

Plan v2 adds SQL-derived physical dependencies and separate execution edges for
hooks, build tests and unit tests. The 17 Core 1.12.6 planning scenarios passed
again, as did Core 1.6.0 and native v2 2.0.9 baseline/repeat smoke checks. Fourteen
local tests cover transport plus lineage, CTE/quoted identifier resolution,
prior-state DML reads, temporary relations, test gates and hook ordering.
The Python running planning checks now needs SQLGlot 30.x.

Run `python compatibility/capture.py --python /path/to/core/python --directory /new/path`.
On Core 1.12.6 / Snowflake 1.12.4 / SQLGlot 30.25.0, all nine offline cases passed:
table/view plus hooks, incremental projection metadata missing/supplied, bound
seed inserts, unit-test projection metadata missing/full SQL generated, data-result
read blocked, statement-response read blocked, and missing metadata blocked.
These use the real Snowflake adapter/macros with connection opening prohibited;
they do not execute against Snowflake. Full unit-test SQL is captured without
pretending the unknown outcome passed.

A separate DuckDB 1.11.1 local canary supplied metadata read from its actual
warehouse, captured `run --select a+`, then executed that command. All 17
model-attributed adapter statements across three models matched the real SQL log
after comment removal and SQL formatting normalization, including transactions,
CREATE, RENAME and DROP. Capture had left only the original input table in the
warehouse before the real run. Setup/introspection SQL is not a complete match:
some adapter metadata methods are replaced by snapshot lookups.

Capture uses internal Core APIs and has only the version coverage stated above.
The capture shim itself is not installed in Cloud or native v2. Neither input freshness, full wire SQL,
query-history matching nor a Nitro consumer is certified by these checks.

## Plan v3 / full TPC-H Cloud verification

[Cloud results](cloud-plan-results.json) record run 522734158 on managed version
2026.10.26+27787ee. The isolated `kuza/compiled-plan-canary` branch of dbt-tpch
ran compile → build → compile over the complete original workload: 24 models
(8 ephemeral, 16 tables), 43 tests and 8 sources. All 16 models built and all 43
tests passed. Both compilations produced equal comparison graph hashes after
explicitly declaring `dbt_batch_id` and `dbt_batch_ts` as invocation parameters.
Original SQL and actual parameter values remain in the plan.

Snowflake query history supplied 59 model/test-attributed workload statements.
All 16 CTAS bodies and all 43 test bodies matched the upfront Cloud compilation.
All 59 full statements also matched local Core 1.12.6 / Snowflake 1.12.4 offline
capture after comment/format normalization and the two declared parameters.
The live catalog collector read both the initial target and the populated target;
capture against the latter generated all 16 model statements without blockers.
The temporary database, job, environment, credentials and extended attributes
were removed after evidence collection. No production job was modified.

This validates this project's workload, not every connection/setup statement,
every Cloud release or runtime installation of the capture shim in Cloud.
No Nitro consumer exists yet; plan equality does not establish input freshness.

Plan v3 also passed the 17 Core 1.12.6 planning cases and 15 local tests. Eleven
real-adapter offline capture cases passed, including the existing unquoted-schema
regression and blocking end hooks that read synthetic execution results.
See the [six-repository public-project survey](public-project-survey.md) for
additional coverage boundaries; that survey is static inspection, not a claim
that all those projects execute successfully through this planner.


## Metadata-backed generation and conditional gates

[Expanded results](expanded-capture-results.json) record eight live Snowflake
incremental cases: 32 DDL/DML statements matched actual execution, and capture
left target data unchanged. Initial/existing snapshots matched three more
statements. Microbatch uses an explicit window and sequential batches;
parallel batches are rejected. Reproduce with `python compatibility/strategies.py
--python /path/to/core/python --database NEW_DISPOSABLE_DATABASE
--directory /new/artifact/path --connection-env SNOWFLAKE_CONNECTION_JSON`.
The harness creates and drops its own database.

Metadata-backed capture uses connector description without executing workloads.
Additional cases cover generated upstream schema introspection, conditional
data/unit test gates, stored failures, schema-only result access, blocking row
reads, native-v2 passing/failing checks and independent component recovery.
The offline suite now has 12 cases; local unit coverage has 16 tests. Full Core
planning passed 17 cases, with partial recovery added to smoke coverage.
Arbitrary adapters, catalog mutations, structured types, runtime telemetry and
Nitro integration remain outside this verification.
