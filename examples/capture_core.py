"""Capture Core adapter submissions offline, stopping on unknown results."""

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from importlib.metadata import version
from threading import Lock
from contextvars import ContextVar


def capture(request):
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import SqlglotError
    from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
    from sqlglot.optimizer.qualify import qualify
    from dbt.cli.main import dbtRunner
    from dbt.clients.jinja import MacroGenerator
    from dbt.context.providers import ProviderContext
    from dbt_common.utils import AttrDict
    from dbt.adapters.factory import get_adapter_class_by_name, load_plugin
    from dbt.adapters.contracts.connection import AdapterResponse
    from dbt.exceptions import DbtRuntimeError
    from dbt_common.clients.agate_helper import empty_table, table_from_data_flat
    from dbt_common.events.contextvars import get_node_info
    from dbt.task import run as run_task
    from dbt.artifacts.schemas.run import RunStatus

    from capture_catalog import GeneratedCatalog

    state = request["warehouse_state"]
    if state["schema"] != "dbt_markers.warehouse_state.v1":
        raise ValueError("Unsupported warehouse state schema")
    adapter_type = state["adapter_type"]
    if adapter_type not in {"duckdb", "snowflake"}:
        raise ValueError("Offline capture currently supports DuckDB and Snowflake Core adapters")
    load_plugin(adapter_type)
    adapter = get_adapter_class_by_name(adapter_type)
    manager = adapter.ConnectionManager
    statements, blockers, lookups = [], [], []
    generated_relations = {}
    generated_catalog = GeneratedCatalog(adapter_type, request.get("describe_connection_env"))
    materialized, post_hooks_finished = set(), set()
    phase = ContextVar("capture_sql_phase", default="adapter")
    lock = Lock()

    def identity():
        return get_node_info().get("unique_id")

    def blocked(reason):
        with lock:
            blockers.append({"node_id": identity(), "reason": reason, "stage": "result_validation" if identity() in materialized and identity() in post_hooks_finished else "sql_generation"})
        raise DbtRuntimeError("Offline capture blocked: " + reason)

    class UnknownResponse(AdapterResponse):
        def __getattribute__(self, name):
            if name in {"code", "rows_affected", "query_id", "_message", "to_dict"}:
                blocked("statement_response_required:" + name)
            return super().__getattribute__(name)

        def __str__(self):
            blocked("statement_response_required:string")

    class UnknownTable:
        def __getattr__(self, name):
            blocked("query_result_required")

        def __getitem__(self, key):
            blocked("query_result_required")

        def __len__(self):
            blocked("query_result_required")

        def __iter__(self):
            blocked("query_result_required")

        def __bool__(self):
            blocked("query_result_required")

        def __str__(self):
            blocked("query_result_required")

        def __eq__(self, other):
            blocked("query_result_required")

        def __ne__(self, other):
            blocked("query_result_required")

        def __contains__(self, value):
            blocked("query_result_required")

    original_store_result = ProviderContext.store_result

    def store_result(self, name, response, agate_table=None):
        if isinstance(agate_table, UnknownTable):
            self.sql_results[name] = AttrDict(response=response, data=agate_table, table=agate_table)
            return ""
        return original_store_result(self, name, response, agate_table)

    contexts = [ProviderContext]
    while contexts:
        context = contexts.pop()
        context._context_members_["store_result"] = store_result
        contexts.extend(context.__subclasses__())

    original_macro_call = MacroGenerator.__call__

    def macro_call(self, *args, **kwargs):
        name = self.macro.name
        current = phase.get()
        if name.startswith("materialization_"):
            current = "materialization"
        elif name == "run_hooks" and args:
            for kind in ("pre", "post"):
                if args[0] is self.context.get(kind + "_hooks"):
                    current = kind + "-hook"
        token = phase.set(current)
        try:
            result = original_macro_call(self, *args, **kwargs)
            if name.startswith("materialization_"):
                materialized.add(identity())
            return result
        finally:
            phase.reset(token)

    MacroGenerator.__call__ = macro_call
    original_post_hook = adapter.post_model_hook

    def post_hook(self, *args, **kwargs):
        result = original_post_hook(self, *args, **kwargs)
        post_hooks_finished.add(identity())
        return result

    adapter.post_model_hook = post_hook

    class UnknownCursor:
        def __getattr__(self, name):
            blocked("cursor_result_required:" + name)

    def relation_key(table):
        return tuple(part.name for part in normalize_identifiers(table.copy(), dialect=adapter_type).parts)

    def record(sql, fetch=False, bindings=None):
        statement = {"node_id": identity(), "phase": phase.get(), "sql": sql, "fetch": fetch, "bindings": bindings, "binding_types": [type(value).__name__ for value in bindings] if bindings is not None else None}
        try:
            parsed = sqlglot.parse(sql, read=adapter_type, error_level=sqlglot.ErrorLevel.RAISE)
        except SqlglotError:
            parsed = []
        with lock:
            statements.append(statement)
            for expression in parsed:
                generated_catalog.record(expression)
                if isinstance(expression, exp.Create):
                    table = expression.this.this if isinstance(expression.this, exp.Schema) else expression.this
                    if isinstance(table, exp.Table):
                        generated_relations[relation_key(table)] = hashlib.sha256(sql.encode()).hexdigest()
        return statement

    def zero_row_projection(sql):
        try:
            statements = sqlglot.parse(sql, read=adapter_type)
            query = statements[0] if len(statements) == 1 else None
            if not isinstance(query, exp.Select):
                return None
            where, limit = query.args.get("where"), query.args.get("limit")
            if not ((where and isinstance(where.this, exp.Boolean) and where.this.this is False)
                    or (limit and isinstance(limit.expression, exp.Literal) and limit.expression.this == "0")):
                return None
            schema = {database: {name: {table: {column["name"]: column["data_type"] for column in data.get("columns", [])}
                                      for table, data in (values["relations"] or {}).items()}
                                 for name, values in details["schemas"].items()}
                      for database, details in state["databases"].items()}
            if any(not isinstance(projection, (exp.Alias, exp.Column, exp.Star))
                   for select in query.find_all(exp.Select) for projection in select.expressions):
                return None
            query = qualify(query, dialect=adapter_type, schema=schema)
            names = query.named_selects
            if not names or any(not name or name == "*" for name in names):
                return None
            return {"columns": names, "rows": []}
        except SqlglotError:
            return None

    def execute(self, sql, auto_begin=False, fetch=False, limit=None):
        if auto_begin and not self.get_thread_connection().transaction_open:
            self.begin()
        statement = record(sql, fetch)
        if fetch:
            supplied = state.get("query_results", {}).get(sql.strip())
            constant = re.fullmatch(r"\s*select\s+([0-9]+)\s*(?:/\*[\s\S]*\*/)?\s*;?\s*", sql, re.IGNORECASE)
            if supplied is None and constant:
                supplied = {"columns": [constant[1]], "rows": [[int(constant[1])]]}
                statement["result_source"] = "constant_expression"
            elif supplied is None:
                supplied = zero_row_projection(sql)
                if supplied is None:
                    statement["result_source"] = "unknown"
                    return UnknownResponse(_message=""), UnknownTable()
                statement["result_source"] = "zero_row_projection"
            else:
                statement["result_source"] = "supplied_snapshot"
                statement["result_kind"] = supplied.get("kind", "data")
            data = [dict(zip(supplied["columns"], row, strict=True)) for row in supplied["rows"]]
            return UnknownResponse(_message=""), table_from_data_flat(data, supplied["columns"])
        return UnknownResponse(_message=""), empty_table()

    def add_query(self, sql, auto_begin=True, bindings=None, abridge_sql_log=False, **kwargs):
        if auto_begin and not self.get_thread_connection().transaction_open:
            self.begin()
        record(sql, bindings=bindings)
        return self.get_thread_connection(), UnknownCursor()

    def identifier(value):
        return value[1:-1].replace('""', '"') if value and value.startswith('"') and value.endswith('"') else value

    def schema_state(database, schema):
        database, schema = identifier(database), identifier(schema)
        with lock:
            lookups.append({"node_id": identity(), "database": database, "schema": schema})
        databases = state["databases"]
        if database not in databases or schema not in databases[database]["schemas"]:
            blocked("schema_metadata_missing:" + str(database) + "." + str(schema))
        return databases[database]["schemas"][schema]

    def list_schemas(self, database):
        database = identifier(database)
        if database not in state["databases"]:
            blocked("database_metadata_missing:" + str(database))
        return list(state["databases"][database]["schemas"])

    def list_relations(self, relation):
        database, schema = relation_key(sqlglot.parse_one(str(relation), read=adapter_type, into=exp.Table))
        relations = schema_state(database, schema)["relations"]
        if relations is None:
            blocked("relation_metadata_missing:" + database + "." + schema)
        return [self.Relation.create(database=database, schema=schema, identifier=name, type=data["type"])
                for name, data in relations.items()]

    def columns(self, relation):
        table = sqlglot.parse_one(str(relation), read=adapter_type, into=exp.Table)
        generated = generated_relations.get(relation_key(table))
        if generated:
            derived = state.get("derived_columns", {}).get(generated)
            if derived is None:
                derived = generated_catalog.describe(table)
            if derived is None:
                blocked("derived_column_metadata_missing:" + generated)
            return [self.Column.create(column["name"], column["data_type"]) for column in derived]
        database, schema, name = relation_key(table)
        relations = schema_state(database, schema)["relations"]
        if name not in relations or "columns" not in relations[name]:
            blocked("column_metadata_missing:" + str(relation))
        return [self.Column.create(column["name"], column["data_type"]) for column in relations[name]["columns"]]

    def no_open(cls, connection):
        blocked("connection_open_forbidden")

    # Response values are never supplied to project macros. Only dbt's reporting
    # boundary receives a success placeholder after generation has finished.
    original_execute = run_task.ModelRunner.execute

    def execute_model(self, model, manifest):
        if model.language != "sql":
            blocked("non_sql_model_capture_unsupported")
        return original_execute(self, model, manifest)

    original_result = run_task.ModelRunner._build_run_model_result

    def model_result(self, model, context, *args, **kwargs):
        result = context["load_result"]("main")
        if result and isinstance(result.response, UnknownResponse):
            result.response = AdapterResponse(_message="CAPTURED; execution not performed")
        return original_result(self, model, context, *args, **kwargs)

    def hook_status(sql, current_adapter):
        if sql.strip():
            current_adapter.execute(sql, auto_begin=False, fetch=False)
        return RunStatus.Success, "CAPTURED; execution not performed"

    original_hook_sql = run_task.RunTask.get_hook_sql

    def hook_sql(self, adapter, hook, idx, num_hooks, extra_context):
        if extra_context and "results" in extra_context:
            extra_context = {**extra_context, "results": UnknownTable()}
        return original_hook_sql(self, adapter, hook, idx, num_hooks, extra_context)

    run_task.RunTask.get_hook_sql = hook_sql
    manager.open = classmethod(no_open)
    manager.execute = execute
    manager.add_query = add_query
    adapter.list_schemas = list_schemas
    adapter.list_relations_without_caching = list_relations
    adapter.get_columns_in_relation = columns
    def check_schema(self, database, schema):
        relation = self.Relation.create(database=database, schema=schema, quote_policy=self.config.quoting).without_identifier()
        database, schema = relation_key(sqlglot.parse_one(str(relation), read=adapter_type, into=exp.Table))
        return schema in list_schemas(self, database)

    adapter.check_schema_exists = check_schema
    run_task.ModelRunner.execute = execute_model
    run_task.ModelRunner._build_run_model_result = model_result
    run_task.get_execution_status = hook_status
    try:
        result = dbtRunner().invoke(request["dbt_args"])
    finally:
        generated_catalog.close()
    return {
        "schema": "dbt_markers.execution_capture.v1",
        "adapter_type": adapter_type,
        "versions": {"dbt_core": version("dbt-core"), "adapter": version("dbt-" + adapter_type), "sqlglot": sqlglot.__version__},
        "command": request["command"],
        "offline": not bool(request.get("describe_connection_env")),
        "workload_executed": False,
        "generated_column_descriptions": generated_catalog.descriptions,
        "submission_boundary": "dbt_adapter",
        "query_comments_omitted": True,
        "assumptions": ["supplied metadata is current", "captured statements succeed if executed"],
        "metadata_queries_replaced_by_snapshot": True,
        "generation_completed": result.success and not blockers,
        "materializations_generated": sorted(key for key in materialized & post_hooks_finished if key is not None and not any(item["node_id"] == key and item["stage"] == "sql_generation" for item in blockers)),
        "statements": statements,
        "blockers": blockers,
        "metadata_lookups": lookups,
        "error": str(result.exception) if result.exception else None,
        "node_results": [{"node_id": item.node.unique_id, "status": str(item.status), "message": item.message}
                         for item in getattr(result.result, "results", [])],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Capture output must be a new path")
    os.environ["DBT_SEND_ANONYMOUS_USAGE_STATS"] = "false"
    request = json.loads(args.request.read_text())
    result = capture(request)
    args.output.write_text(json.dumps(result, indent=2, default=str) + "\n")
    print(json.dumps({"generation_completed": result["generation_completed"], "statements": len(result["statements"]), "blockers": result["blockers"]}))
