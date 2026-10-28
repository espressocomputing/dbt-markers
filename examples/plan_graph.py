"""Physical SQL lineage and execution constraints for compiled dbt plans."""

import hashlib
import json
from graphlib import CycleError, TopologicalSorter

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.scope import traverse_scope


DIALECTS = {"snowflake": "snowflake", "duckdb": "duckdb", "postgres": "postgres",
            "redshift": "redshift", "bigquery": "bigquery", "databricks": "databricks",
            "spark": "spark", "sqlserver": "tsql"}


def relation_parts(table, dialect, database=None, schema=None):
    table = normalize_identifiers(table.copy(), dialect=dialect)
    if not isinstance(table.this, exp.Identifier):
        raise ValueError("dynamic_relation")
    parts = [part.name for part in table.parts]
    if len(parts) == 1 and schema:
        parts.insert(0, schema)
    if len(parts) == 2 and database:
        parts.insert(0, database)
    if len(parts) != 3:
        raise ValueError("unqualified_relation:" + table.sql(dialect=dialect))
    return tuple(parts)


def physical_reads(sql, dialect, database=None, schema=None):
    reads, issues, produced = set(), set(), set()
    try:
        statements = sqlglot.parse(sql, read=dialect, error_level=sqlglot.ErrorLevel.RAISE)
        for statement in statements:
            if statement is None:
                continue
            if isinstance(statement, exp.Command):
                issues.add("opaque_sql_statement")
            if list(statement.find_all(exp.Anonymous)):
                issues.add("function_body_not_inspected")
            cte_refs = set()
            for scope in traverse_scope(statement):
                for reference, source in scope.selected_sources.values():
                    if not isinstance(source, exp.Table):
                        cte_refs.add(id(reference))
            target = statement.args.get("this")
            if isinstance(target, exp.Schema):
                target = target.this
            write_only = isinstance(statement, (exp.Create, exp.Drop, exp.Alter))
            root_with = statement.args.get("with_")
            root_ctes = {cte.alias for cte in root_with.expressions} if root_with else set()
            for table in statement.find_all(exp.Table):
                if id(table) in cte_refs or (write_only and table is target):
                    continue
                if table is not target and not table.db and table.name in root_ctes and table.find_ancestor(exp.CTE) is None:
                    continue
                try:
                    parts = relation_parts(table, dialect, database, schema)
                    if parts not in produced:
                        reads.add(parts)
                except ValueError as error:
                    issues.add(str(error))
            if isinstance(statement, exp.Create) and isinstance(target, exp.Table):
                try:
                    produced.add(relation_parts(target, dialect, database, schema))
                except ValueError as error:
                    issues.add(str(error))
    except SqlglotError as error:
        issues.add("sql_parse_error:" + type(error).__name__)
    return sorted(reads), sorted(issues)


def add_lineage(nodes, adapter, database=None, schema=None):
    dialect = DIALECTS.get(adapter)
    if dialect is None:
        for node in nodes.values():
            if node["compiled_sql"]:
                node["unresolved"].append("unsupported_sql_dialect:" + adapter)
        return
    by_relation = {}
    for key, node in nodes.items():
        if not node["relation_name"]:
            continue
        try:
            relation = sqlglot.parse_one(node["relation_name"], read=dialect, into=exp.Table)
            parts = relation_parts(relation, dialect, database, schema)
        except (SqlglotError, ValueError):
            node["unresolved"].append("unparseable_node_relation")
            continue
        by_relation.setdefault(parts, []).append(key)
    for key, node in list(nodes.items()):
        node["declared_depends_on"] = list(node["depends_on"])
        node["sql_depends_on"] = []
        node["input_relations"] = []
        texts = ([node["compiled_sql"]] if node["compiled_sql"] else []) + [statement["sql"] for statement in node.get("generated_statements", []) if statement.get("result_kind") != "metadata" and statement.get("result_source") != "zero_row_projection"]
        if not texts:
            continue
        reads, issues = physical_reads(";\n".join(texts), dialect, database, schema)
        node["unresolved"].extend(issues)
        for parts in reads:
            node["input_relations"].append(list(parts))
            candidates = by_relation.get(parts, [])
            prior_state = key in candidates or node.get("hook_owner") in candidates
            if prior_state:
                candidates = []
            producers = [candidate for candidate in candidates if nodes[candidate]["resource_type"] in {"model", "seed", "snapshot"}]
            declared = [candidate for candidate in candidates if candidate in node["declared_depends_on"]]
            if len(producers) == 1:
                parent = producers[0]
            elif len(declared) == 1:
                parent = declared[0]
            elif len(candidates) == 1:
                parent = candidates[0]
            elif candidates:
                node["unresolved"].append("ambiguous_relation:" + ".".join(parts))
                continue
            else:
                parent = "relation." + hashlib.sha256(json.dumps([parts, key if prior_state else None]).encode()).hexdigest()
                nodes.setdefault(parent, {
                    "resource_type": "external_relation", "state": "before_node" if prior_state else "external", "depends_on": [],
                    "declared_depends_on": [], "sql_depends_on": [], "input_relations": [],
                    "relation_name": sqlglot.exp.table_(parts[2], db=parts[1], catalog=parts[0], quoted=True).sql(dialect=dialect),
                    "database": parts[0], "schema": parts[1], "identifier": parts[2],
                    "language": "sql", "raw_code": None, "compiled_sql": None,
                    "config": {}, "unresolved": [],
                })
            node["sql_depends_on"].append(parent)
        node["sql_depends_on"] = sorted(set(node["sql_depends_on"]))
        node["depends_on"] = sorted(set(node["depends_on"]) | set(node["sql_depends_on"]))
    try:
        list(TopologicalSorter({key: node["depends_on"] for key, node in nodes.items()}).static_order())
    except CycleError as error:
        for key in set(error.args[1]):
            nodes[key]["unresolved"].append("sql_lineage_cycle")


def add_hooks(nodes):
    for owner, node in list(nodes.items()):
        for phase in ("pre-hook", "post-hook"):
            for index, hook in enumerate(node["config"].get(phase, [])):
                hook = {"sql": hook} if isinstance(hook, str) else hook
                sql = hook["sql"]
                dynamic = "{{" in sql or "{%" in sql or "{#" in sql
                key = f"hook.{owner}.{phase}.{index}"
                nodes[key] = {
                    "resource_type": "hook", "depends_on": [], "relation_name": None,
                    "database": node["database"], "schema": node["schema"], "identifier": None,
                    "language": "sql", "raw_code": sql, "compiled_sql": None if dynamic else sql,
                    "config": {"transaction": hook.get("transaction", True)},
                    "hook_owner": owner, "hook_phase": phase, "hook_index": index,
                    "unresolved": ["hook_requires_runtime_render"] if dynamic else [],
                }


def execution_edges(nodes, selected, command, project_name):
    selected = set(selected)
    edges = {(parent, key, "data") for key, node in nodes.items() for parent in node["depends_on"] if node["resource_type"] != "unit_test"}
    if command == "build":
        for child in selected:
            if nodes[child]["resource_type"] in {"test", "unit_test"}:
                continue
            ancestors, pending = set(), list(nodes[child]["declared_depends_on"])
            while pending:
                parent = pending.pop()
                if parent not in ancestors:
                    ancestors.add(parent)
                    pending.extend(nodes[parent].get("declared_depends_on", []))
            for test in selected:
                parents = set(nodes[test]["declared_depends_on"])
                if nodes[test]["resource_type"] == "test" and parents and parents <= ancestors:
                    edges.add((test, child, "test_gate"))
        for unit in selected:
            if nodes[unit]["resource_type"] == "unit_test":
                for model in nodes[unit]["declared_depends_on"]:
                    for parent in nodes[model]["declared_depends_on"]:
                        edges.add((parent, unit, "unit_test_setup"))
                    if model in selected:
                        edges.add((unit, model, "unit_test_gate"))
    boundaries = {}
    for owner in list(selected):
        for phase in ("pre-hook", "post-hook"):
            hooks = sorted((key for key, node in nodes.items() if node.get("hook_owner") == owner and node["hook_phase"] == phase), key=lambda key: nodes[key]["hook_index"])
            selected.update(hooks)
            if hooks:
                boundaries.setdefault(owner, [owner, owner])[0 if phase == "pre-hook" else 1] = hooks[0] if phase == "pre-hook" else hooks[-1]
            sequence = [*hooks, owner] if phase == "pre-hook" else [owner, *hooks]
            edges.update((left, right, "hook_order") for left, right in zip(sequence, sequence[1:]))
            if hooks and phase == "pre-hook":
                edges.update((parent, hooks[0], "hook_order") for parent in nodes[owner]["depends_on"])
            if hooks and phase == "post-hook":
                edges.update((hooks[-1], child, "hook_order") for child in selected if owner in nodes[child]["depends_on"])
    edges.update((boundaries.get(left, [left, left])[1], boundaries.get(right, [right, right])[0], kind)
                 for left, right, kind in list(edges) if kind != "hook_order")
    for phase in ("on-run-start", "on-run-end"):
        hooks = sorted((key for key, node in nodes.items() if phase in node.get("tags", [])), key=lambda key: (nodes[key].get("package_name") == project_name, nodes[key].get("package_name", ""), nodes[key].get("hook_index", 0)))
        edges.update((left, right, "project_hook_order") for left, right in zip(hooks, hooks[1:]))
        work = {key for key in selected if nodes[key]["resource_type"] != "operation"}
        if hooks:
            edges.update((hooks[-1], key, "project_hook_order") if phase == "on-run-start" else (key, hooks[0], "project_hook_order") for key in work)
            selected.update(hooks)
    return [{"from": left, "to": right, "kind": kind} for left, right, kind in sorted(edges)], sorted(selected)
