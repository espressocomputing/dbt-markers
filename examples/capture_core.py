"""Capture Core adapter submissions offline, stopping on unknown results."""

import argparse
import json
import os
from pathlib import Path
from threading import Lock


def capture(request):
    from dbt.cli.main import dbtRunner
    from dbt.adapters.factory import get_adapter_class_by_name, load_plugin
    from dbt.adapters.contracts.connection import AdapterResponse
    from dbt.exceptions import DbtRuntimeError
    from dbt_common.clients.agate_helper import empty_table, table_from_data_flat
    from dbt_common.events.contextvars import get_node_info
    from dbt.task import run as run_task
    from dbt.artifacts.schemas.run import RunStatus

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
    lock = Lock()

    def identity():
        return get_node_info().get("unique_id")

    def blocked(reason):
        with lock:
            blockers.append({"node_id": identity(), "reason": reason})
        raise DbtRuntimeError("Offline capture blocked: " + reason)

    class UnknownResponse(AdapterResponse):
        def __getattribute__(self, name):
            if name in {"code", "rows_affected", "query_id", "_message", "to_dict"}:
                blocked("statement_response_required:" + name)
            return super().__getattribute__(name)

        def __str__(self):
            blocked("statement_response_required:string")

    class UnknownCursor:
        def __getattr__(self, name):
            blocked("cursor_result_required:" + name)

    def record(sql, fetch=False, bindings=None):
        with lock:
            statements.append({"node_id": identity(), "sql": sql, "fetch": fetch, "bindings": bindings})

    def execute(self, sql, auto_begin=False, fetch=False, limit=None):
        record(sql, fetch)
        if fetch:
            supplied = state.get("query_results", {}).get(sql.strip())
            if supplied is None:
                blocked("query_result_required")
            statements[-1]["result_source"] = "supplied_snapshot"
            return UnknownResponse(_message=""), table_from_data_flat(supplied["rows"], supplied["columns"])
        return UnknownResponse(_message=""), empty_table()

    def add_query(self, sql, auto_begin=True, bindings=None, abridge_sql_log=False, **kwargs):
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
        relations = schema_state(relation.database, relation.schema)["relations"]
        return [self.Relation.create(database=relation.database, schema=relation.schema, identifier=name, type=data["type"])
                for name, data in relations.items()]

    def columns(self, relation):
        relations = schema_state(relation.database, relation.schema)["relations"]
        if relation.identifier not in relations or "columns" not in relations[relation.identifier]:
            blocked("column_metadata_missing:" + str(relation))
        return [self.Column.create(column["name"], column["data_type"]) for column in relations[relation.identifier]["columns"]]

    def no_open(cls, connection):
        blocked("connection_open_forbidden")

    # Response values are never supplied to project macros. Only dbt's reporting
    # boundary receives a success placeholder after generation has finished.
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

    manager.open = classmethod(no_open)
    manager.execute = execute
    manager.add_query = add_query
    adapter.list_schemas = list_schemas
    adapter.list_relations_without_caching = list_relations
    adapter.get_columns_in_relation = columns
    adapter.check_schema_exists = lambda self, database, schema: identifier(schema) in list_schemas(self, database)
    run_task.ModelRunner._build_run_model_result = model_result
    run_task.get_execution_status = hook_status
    result = dbtRunner().invoke(request["dbt_args"])
    return {
        "schema": "dbt_markers.execution_capture.v1",
        "adapter_type": adapter_type,
        "command": request["command"],
        "offline": True,
        "assumptions": ["supplied metadata is current", "captured statements succeed if executed"],
        "metadata_queries_replaced_by_snapshot": True,
        "generation_completed": result.success and not blockers,
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
