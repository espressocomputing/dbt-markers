# Native dbt capture investigation

Installed runtime: dbt 2.0.9. Its Python CLI delegates to `dbt._core.run_cli`;
`dbt.runner.dbtRunner(callbacks=...)` explicitly raises `NotImplementedError`.
Python Core adapter/model-runner interception is unavailable.

Source inspected at dbt-labs/dbt commit
[e9f2bf8c525808d5a82d908bf2ab87681aa5c58a](https://github.com/dbt-labs/dbt/tree/e9f2bf8c525808d5a82d908bf2ab87681aa5c58a):

- `crates/dbt-clap-core/src/lib.rs` exposes hidden Time Machine record/replay
  flags. Recording executes the workload; replay requires recorded events.
- `crates/dbt-adapter/src/time_machine/engine.rs` requires matching write events
  and recorded results. It is not an upfront capture mode for changed workloads.
- `crates/dbt-compilation/src/core.rs` uses mock adapters for no-introspection
  compilation, with DuckDB/LakeCompute exceptions. Adapter mocks can return
  placeholders or ignore mutations. This does not establish accurate materialized
  SQL or a general no-side-effects execution boundary.

Do not substitute a Python Core run for native materialization and call its SQL
exact. Native artifact import is supported without invoking the engine. Full
native upfront materialization capture remains unsupported and is rejected
before project execution when requested through the Python hook path.

A native implementation needs an engine interception point spanning compilation,
metadata and materialization submissions. It must preserve original SQL/bindings,
resolve supplied metadata, represent unavailable results without invented rows,
and stop when those results affect generation. A query logging callback alone
would observe writes after execution and cannot satisfy the capture contract.

The Python generator now runs compilation under its existing interception too;
there is no unguarded compile fallback. Trusted custom plugins can still bypass
standard adapters; this mechanism is not an operating-system sandbox.


## Implemented package path

The startup hook now exports additional source/configuration context without
rendering project Jinja. Query comments carry available compiled bodies as
ordinary statements pass through the proxy. `collect_capture` preserves those
observations and original statement order. Customer command lines stay unchanged
following the existing one-time marker/hook setup.

The receiver-side partial renderer evaluates a deliberately restricted AST subset
without invoking project functions. It emits candidates and explicit holes; it
never fabricates row values or treats parse-time stub values as real results.
Native symbolic lint supports hole propagation internally, but its tested CLI
only returns lint diagnostics, not a partial-SQL capture artifact. We have not
exposed or reimplemented its full macro renderer.

The remaining native-engine work is exporting that richer symbolic representation
or adding an execution-interception API. The implemented package path does not
promise complete upfront materialization statements.
