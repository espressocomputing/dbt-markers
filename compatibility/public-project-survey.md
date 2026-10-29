# Public dbt project survey — 2026-10-28

The prototype covers a useful recurring subset: ordinary SQL model bodies, refs,
sources, table/view/ephemeral dependencies and standard test bodies. This survey
does **not** establish broad project compatibility or a market coverage percentage.
Metadata-driven generation is frequent in the larger projects; data-driven macros,
microbatch orchestration and result-driven end hooks are real workloads, not just
hypothetical edge cases. A general warehouse simulator is not the next step.

Six public repositories were inspected at pinned commits: two example/production
projects and four packages used inside projects. The five repositories with models
contain 979 model SQL files; Project Evaluator v2 instead has 28 local check SQL
files. These are source files, not enabled nodes: flags can select alternative API
implementations, and packages can add nodes. We read configs, version evidence,
representative models, macro implementations and CI definitions. No public project
was executed against its warehouse. The Lightdash Jaffle Shop fork was excluded
because we did not establish a recent dbt-version signal. This is a deliberately
varied convenience sample, not a statistically representative sample.

| Repository | Recent-version evidence | Inspected workload | Assessment |
| --- | --- | --- | --- |
| [dbt-labs/jaffle-shop](https://github.com/dbt-labs/jaffle-shop/blob/5beb145b00f5465ec759cfcdd9745e858818cf95/dbt_project.yml) | Requires dbt ≥2.0 | 13 models; view/table layers, seeds, unit tests and semantic definitions | SQL-model pattern fits compilation; native-v2 materialization capture and semantic-query generation are not covered. |
| [Cal-ITP data-infra](https://github.com/cal-itp/data-infra/blob/472c85e696ec2f8532d0ea90c9edb28069701fa9/warehouse/pyproject.toml) | Core 1.10.8 / BigQuery 1.10.2 pinned | 626 models; 62 explicitly incremental, including 47 microbatch and 13 insert-overwrite; five direct get_column_values models | Many ordinary SQL bodies, but BigQuery execution capture is unsupported and dynamic branches require results. |
| [Fivetran Shopify](https://github.com/fivetran/dbt_shopify/blob/56ee0ff062e47aa751da77d6b76e42689f2e69e4/dbt_project.yml) | Active October 2026; permits ≥1.3,<3; CI scenario file discusses v1/v2 ranges, not an exact installed-version pin | 264 model files across REST/GraphQL alternatives; 102 model files call catalog macros directly; 12 call the metafield macro | Strong motivation for live catalog support. Metafield pivots genuinely depend on data values; cannot promise a full upfront snapshot from catalog alone. |
| [Elementary](https://github.com/elementary-data/dbt-data-reliability/blob/e3ab23d8c2e3bd7ad455de8bb1d23bf7ee84e675/.github/workflows/test-all-warehouses.yml) | CI targets latest official/prerelease Core and Fusion | 30 models plus runtime observation macros, custom materializations and start/end hooks | Model SQL is only part of its work. Outcome/response-derived SQL remains conditional or blocked. |
| [dbt_artifacts](https://github.com/brooklyn-data/dbt_artifacts/blob/16662c081b64da17651b811135eff82b3bfd405b/.github/workflows/release.yml) | CI explicitly includes Snowflake/BigQuery Core 1.9, 1.10 and 1.11 | 46 models; incremental artifact storage and results uploads | Reporting bodies fit compilation; upload inserts contain actual execution results unavailable upfront. |
| [dbt-project-evaluator v2](https://github.com/dbt-labs/dbt-project-evaluator/blob/8b505558ed3865debb5cf0fe12ba6b313425e8f0/dbt_project.yml) | Requires ≥2.0,<3 | 28 native checks instead of warehouse models | These run locally and can block build. They are validation gates, not missing warehouse SELECTs; our gate model does not yet verify them. |

## Concrete boundaries

**Catalog-driven is different from data-driven.** Shopify's schema expansion
includes adapter column reads and `dbt_utils.star`. These are reasonable targets
for real catalog inspection, but intermediate relations may not exist yet or may
change during the run. Its metafield macro separately reads distinct field values
and turns those rows into output columns; actual data is required. Twelve models
call it, although configuration may disable some. [Metafield implementation](https://github.com/fivetran/dbt_shopify/blob/56ee0ff062e47aa751da77d6b76e42689f2e69e4/macros/get_metafields.sql); [example caller](https://github.com/fivetran/dbt_shopify/blob/56ee0ff062e47aa751da77d6b76e42689f2e69e4/models/rest/metafields/shopify__customer_metafields.sql).

Cal-ITP similarly reads exported timestamps with `get_column_values` and embeds
the selected value into generated SQL. A live catalog cannot supply that value.
Its incremental model configs also make microbatch and partition replacement
important coverage gaps; a single model does not necessarily map to one SQL
statement. [Data-derived timestamp](https://github.com/cal-itp/data-infra/blob/472c85e696ec2f8532d0ea90c9edb28069701fa9/warehouse/models/intermediate/payments/int_payments__dim_product_data.sql); [microbatch model](https://github.com/cal-itp/data-infra/blob/472c85e696ec2f8532d0ea90c9edb28069701fa9/warehouse/models/mart/kuba/fct_kuba__device_properties.sql).

**Execution metadata is not known just because source-table data is unnecessary.**
The dbt_artifacts end hook uploads `results`, including runtime execution details.
Elementary reads `load_result("main")` to decide whether a test passed. Those paths
cannot consume invented successful results. The survey exposed a gap in our
capture reporting shim: end hooks could see its synthetic model result objects.
Capture now substitutes an unknown result object for end-hook rendering and blocks
when the hook reads it. Static end hooks can still generate SQL. [dbt_artifacts hook setup](https://github.com/brooklyn-data/dbt_artifacts/blob/16662c081b64da17651b811135eff82b3bfd405b/README.md); [upload implementation](https://github.com/brooklyn-data/dbt_artifacts/blob/16662c081b64da17651b811135eff82b3bfd405b/macros/upload_results/upload_results.sql); [Elementary result inspection](https://github.com/elementary-data/dbt-data-reliability/blob/e3ab23d8c2e3bd7ad455de8bb1d23bf7ee84e675/macros/edr/materializations/test/test_result.sql).

**Modern dbt adds execution gates outside warehouse SQL.** Jaffle Shop contains
unit tests and embedded semantic definitions. Project Evaluator v2 moved from
warehouse models to local information-schema checks that can prevent a build.
Our warehouse DAG must not be presented as the entire modern dbt execution graph.
[Jaffle Shop model definitions](https://github.com/dbt-labs/jaffle-shop/blob/5beb145b00f5465ec759cfcdd9745e858818cf95/models/marts/orders.yml); [native-check execution contract](https://github.com/dbt-labs/dbt-project-evaluator/blob/8b505558ed3865debb5cf0fe12ba6b313425e8f0/README.md).

## What to claim and what to add

- Verified today: dbt-tpch on Cloud, with all 24 compiled model bodies, 43 test
  bodies, and all 59 materialization/test statements matching offline capture
  after explicitly declared parameter and formatting normalization. This is
  stronger evidence than the static public-project survey, but applies to that
  workload and the recorded engine versions.
- Supported design subset: ordinary SQL bodies and declared/physical dependencies,
  with standard table/view/test generation on the verified Snowflake Core path.
  Equivalent patterns in other projects are candidates, not certified runs.
- Subsequent live fixtures cover standard incrementals, schema evolution,
  sequential microbatch, snapshots, metadata-only introspection and stored test
  failures. See [expanded evidence](expanded-capture-results.json). These fixtures
  do not certify the surveyed projects or every package macro.
- Independent selected components can survive unrelated compilation failures;
  capture can compile against generated upstream schemas. Missing bodies and
  untraced compile-time database reads remain explicit.
- Keep result-dependent branches and telemetry hooks on actual execution. Never
  normalize arbitrary timestamps, UUIDs or business literals to force equality.
  The new invocation-column contract is opt-in and retains original SQL/values.
- Native-v2 checks can run as separate validation gates with
  `--validate-native-checks`. Do not count semantic definitions as already-generated
  warehouse queries.

No snapshots or Python-model coverage is established by this sample. No attempt
was made to infer adoption shares or transitive compiled coverage from grep counts.
Pinned inventory and search counts are in [public-project-inventory.json](public-project-inventory.json).
