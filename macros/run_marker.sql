{#- In-process dbt (dbtRunner) builds invocation_command from the host's argv, so the query
    comment can miss the real command: record dbt's own arguments once per run. -#}
{% macro run_marker() -%}
    {%- set run = {"invocation_id": invocation_id, "args": invocation_args_dict} -%}
    select 1 /* {{ tojson({"dbt_markers_run": run}) | replace("/*", "\\u002f*") | replace("*/", "*\\/") }} */
{%- endmacro %}
