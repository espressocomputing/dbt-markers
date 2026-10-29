{#
    dbt_markers.query_comment(node, extra={})

    Returns one single-line JSON object describing the dbt invocation and, when
    there is one, the node behind the statement. Use it in dbt_project.yml:

        query-comment:
          comment: "{{ dbt_markers.query_comment(node) }}"
          append: true

    Runs no SQL and records what dbt knows without interpreting it. String keys
    are written only when the value is a non-empty string (never null);
    full_refresh is a boolean; node_tags, node_ref_names and node_depends_on are lists
    of non-empty strings; node_meta is passed through as dbt provides it.
#}
{% macro query_comment(node, extra={}) -%}
    {%- set has_node = node is defined and node is not none -%}

    {#- invocation_command: the executable becomes "dbt", the value after --vars is
        redacted up to the next flag (-x / --xyz), and whitespace collapses to one space.
        invocation_vars keeps the redacted value verbatim. -#}
    {%- set command = flags.INVOCATION_COMMAND -%}
    {%- set words = ["dbt"] -%}
    {%- set vars_words = [] -%}
    {%- set state = namespace(redacting=false) -%}
    {%- for word in (command.split()[1:] if command is string else []) -%}
        {%- if word == "--vars" or word.startswith("--vars=") -%}
            {%- do words.append("--vars <redacted>") -%}
            {%- do vars_words.append(word[7:]) if word.startswith("--vars=") -%}
            {%- set state.redacting = true -%}
        {%- elif not state.redacting or modules.re.match("--?[A-Za-z]", word) -%}
            {%- do words.append(word) -%}
            {%- set state.redacting = false -%}
        {%- else -%}
            {%- do vars_words.append(word) -%}
        {%- endif -%}
    {%- endfor -%}

    {#- Objects read one level deeper; each is used only when defined and not a string. -#}
    {%- set config = node.config if has_node and node.config is defined and node.config is not string else none -%}
    {%- set depends_on = node.depends_on if has_node and node.depends_on is defined and node.depends_on is not string else none -%}
    {%- set checksum = node.checksum if has_node and node.checksum is defined and node.checksum is not string else none -%}
    {%- set is_seed = has_node and node.resource_type == "seed" -%}

    {#- node_ref_names: ref() target names, first occurrence kept. node_depends_on: parent ids.
        Both are [] on seeds, whose refs cannot be read. -#}
    {%- set refs = [] -%}
    {%- if has_node and not is_seed and node.refs is iterable and node.refs is not string -%}
        {%- for ref in node.refs if ref.name is string and ref.name != "" and ref.name not in refs -%}
            {%- do refs.append(ref.name) -%}
        {%- endfor -%}
    {%- endif -%}
    {%- set parents = [] -%}
    {%- if depends_on is not none and not is_seed and depends_on.nodes is defined
          and depends_on.nodes is iterable and depends_on.nodes is not string and depends_on.nodes is not mapping -%}
        {%- for parent in depends_on.nodes if parent is string and parent != "" -%}
            {%- do parents.append(parent) -%}
        {%- endfor -%}
    {%- endif -%}
    {%- set tags = none -%}
    {%- if has_node and node.tags is defined and node.tags is iterable and node.tags is not string and node.tags is not mapping -%}
        {%- set tags = [] -%}
        {%- for tag in node.tags if tag is string and tag != "" -%}
            {%- do tags.append(tag) -%}
        {%- endfor -%}
    {%- endif -%}
    {%- set config_meta = config.meta if config is not none and config.meta is defined else none -%}
    {%- set node_meta = node.meta if has_node and node.meta is defined else none -%}

    {#- Every key in output order. Keys in `typed` are written when not none; all others
        only when the value is a non-empty string. -#}
    {%- set typed = ["full_refresh", "node_ref_names", "node_depends_on", "node_tags", "node_meta"] -%}
    {%- set fields = {
        "dbt_markers": "1.1.0",
        "invocation_id": invocation_id,
        "which": flags.WHICH,
        "invocation_command": words | join(" ") if command is string and command.strip() != "" else none,
        "invocation_vars": vars_words | join(" "),
        "full_refresh": flags.FULL_REFRESH if flags.FULL_REFRESH is boolean else none,
        "run_started_at": run_started_at.isoformat() if run_started_at is defined and run_started_at is not none else none,
        "dbt_version": dbt_version,
        "project_name": project_name,
        "target_name": target.name,
        "target_database": target.database,
        "target_schema": target.schema,
        "node_unique_id": node.unique_id if has_node else none,
        "node_resource_type": node.resource_type if has_node else none,
        "node_package_name": node.package_name if has_node else none,
        "node_name": node.name if has_node else none,
        "node_alias": node.alias if has_node else none,
        "node_original_file_path": node.original_file_path if has_node else none,
        "node_database": node.database if has_node else none,
        "node_schema": node.schema if has_node else none,
        "materialized": config.materialized if config is not none else none,
        "incremental_strategy": config.incremental_strategy if config is not none else none,
        "snowflake_warehouse": config.snowflake_warehouse if config is not none else none,
        "node_ref_names": refs if has_node else none,
        "node_depends_on": parents if has_node else none,
        "node_checksum": checksum.checksum if checksum is not none else none,
        "raw_code_md5": local_md5(node.raw_code) if has_node and node.raw_code is string and node.raw_code != "" else none,
        "node_tags": tags,
        "node_meta": config_meta if config_meta is mapping else (node_meta if node_meta is mapping else none),
    } -%}
    {%- for name in ["DBT_CLOUD_ACCOUNT_ID", "DBT_CLOUD_PROJECT_ID", "DBT_CLOUD_ENVIRONMENT_ID",
                     "DBT_CLOUD_ENVIRONMENT_NAME", "DBT_CLOUD_ENVIRONMENT_TYPE", "DBT_CLOUD_JOB_ID",
                     "DBT_CLOUD_RUN_ID", "DBT_CLOUD_RUN_REASON", "DBT_CLOUD_RUN_REASON_CATEGORY",
                     "DBT_CLOUD_INVOCATION_CONTEXT", "DBT_CLOUD_PR_ID", "DBT_CLOUD_GIT_SHA",
                     "DBT_CLOUD_GIT_BRANCH"] -%}
        {%- do fields.update({name | lower: env_var(name, "")}) -%}
    {%- endfor -%}

    {%- set out = {} -%}
    {%- for key, value in fields.items() -%}
        {%- if (key in typed and value is not none) or (value is string and value != "") -%}
            {%- do out.update({key: value}) -%}
        {%- endif -%}
    {%- endfor -%}

    {#- extra can only add keys; kept values are non-empty strings, booleans, integers
        and finite floats. -#}
    {%- for key, value in (extra.items() if extra is mapping else []) -%}
        {%- if key is string and key != "" and key not in fields
              and ((value is string and value != "") or value is boolean or value is integer
                   or (value is float and value * 0 == 0)) -%}
            {%- do out.update({key: value}) -%}
        {%- endif -%}
    {%- endfor -%}

    {#- Escape "/*" and "*/" so the comment can neither nest nor end early. -#}
    {{- tojson(out) | replace("/*", "\\u002f*") | replace("*/", "*\\/") -}}
{%- endmacro %}
