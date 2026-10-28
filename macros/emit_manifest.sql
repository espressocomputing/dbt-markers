{% macro emit_manifest() -%}
    {%- set mode = var("dbt_markers_manifest_mode", "auto") -%}
    {%- if mode not in ["auto", "required", "off"] -%}
        {{ exceptions.raise_compiler_error("dbt_markers_manifest_mode must be auto, required, or off") }}
    {%- endif -%}
    {%- if execute and mode != "off" -%}
        {%- set state = namespace(reason="") -%}
        {%- if graph is not defined or graph is not mapping -%}
            {%- set state.reason = "runtime graph unavailable" -%}
        {%- elif selected_resources is not defined -%}
            {%- set state.reason = "selected_resources unavailable; use on-run-start" -%}
        {%- elif flags.WHICH in ["build", "test"] and "unit_tests" not in graph
              and not (dbt_version.split('.')[0] == '1' and dbt_version.split('.')[1] | int < 8) -%}
            {%- set state.reason = "runtime graph cannot describe unit-test execution for build/test" -%}
        {%- endif -%}
        {%- set nodes = {} -%}
        {%- if not state.reason -%}
        {%- for collection in ["nodes", "sources", "exposures", "metrics", "semantic_models", "saved_queries", "unit_tests", "functions"] -%}
            {%- for id, node in graph.get(collection, {}).items() | sort -%}
                {%- set record = {
                    "resource_type": node.resource_type,
                    "depends_on": node.get("depends_on", {}).get("nodes", []) | sort,
                } -%}
                {%- for field in ["name", "package_name", "database", "schema", "alias", "identifier", "relation_name"] -%}
                    {%- if node.get(field) is not none -%}
                        {%- do record.update({field: node[field]}) -%}
                    {%- endif -%}
                {%- endfor -%}
                {%- if node.get("config", {}).get("materialized") is not none -%}
                    {%- do record.update({"materialized": node.config.materialized}) -%}
                {%- endif -%}
                {%- do nodes.update({id: record}) -%}
            {%- endfor -%}
        {%- endfor -%}
        {%- set referenced = selected_resources | list -%}
        {%- for node in nodes.values() -%}
            {%- do referenced.extend(node.depends_on) -%}
        {%- endfor -%}
        {%- set missing = [] -%}
        {%- for id in referenced if id not in nodes and id not in missing -%}
            {%- do missing.append(id) -%}
        {%- endfor -%}
        {%- if missing -%}
            {%- set state.reason = "runtime graph omits selected nodes or dependencies: " ~ missing | join(", ") -%}
        {%- endif -%}
        {%- endif -%}
        {%- if state.reason -%}
            {%- if mode == "required" -%}
                {{ exceptions.raise_compiler_error(state.reason) }}
            {%- else -%}
                {%- do log("dbt-markers: graph unavailable; retaining per-query markers: " ~ state.reason, info=true) -%}
                {%- set status = tojson({
                    "dbt_markers_manifest_status": 1,
                    "invocation_id": invocation_id,
                    "status": "unavailable",
                    "reason": state.reason,
                }) | replace("/*", "\\u002f*") | replace("*/", "*\\/") -%}
                {%- do run_query("select 1 /* " ~ status ~ " */") -%}
            {%- endif -%}
        {%- else -%}
        {%- set payload = tojson({
            "schema": "dbt_markers.graph.v1",
            "invocation_id": invocation_id,
            "project_name": project_name,
            "dbt_version": dbt_version,
            "command": flags.WHICH,
            "target_name": target.name,
            "available_collections": graph.keys() | list | sort,
            "selected_resources": selected_resources | sort,
            "nodes": nodes,
        }) -%}
        {%- set manifest_id = local_md5(payload) -%}
        {%- set chunk_size = 16000 -%}
        {%- set chunk_count = (payload | length + chunk_size - 1) // chunk_size -%}
        {%- for index in range(chunk_count) -%}
            {%- set envelope = tojson({
                "dbt_markers_manifest": 1,
                "invocation_id": invocation_id,
                "manifest_id": manifest_id,
                "chunk_index": index,
                "chunk_count": chunk_count,
                "data": payload[index * chunk_size:(index + 1) * chunk_size],
            }) | replace("/*", "\\u002f*") | replace("*/", "*\\/") -%}
            {%- do run_query("select 1 /* " ~ envelope ~ " */") -%}
        {%- endfor -%}
        {%- endif -%}
    {%- endif -%}
{%- endmacro %}
