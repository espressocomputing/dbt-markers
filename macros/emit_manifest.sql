{% macro emit_manifest() -%}
    {%- if execute -%}
        {%- if selected_resources is not defined -%}
            {{ exceptions.raise_compiler_error("Use dbt_markers.emit_manifest() in on-run-start") }}
        {%- endif -%}
        {%- set nodes = {} -%}
        {%- for collection in ["nodes", "sources", "exposures", "metrics", "semantic_models", "saved_queries", "unit_tests"] -%}
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
        {%- set payload = tojson({
            "schema": "dbt_markers.graph.v1",
            "invocation_id": invocation_id,
            "project_name": project_name,
            "dbt_version": dbt_version,
            "command": flags.WHICH,
            "target_name": target.name,
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
{%- endmacro %}
