"""Explicit output-column parameters for comparison without changing captured SQL."""

import copy

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError


def parameterize(sql, columns, dialect):
    if not sql or not columns:
        return sql, []
    try:
        statements = sqlglot.parse(sql, read=dialect, error_level=sqlglot.ErrorLevel.RAISE)
    except SqlglotError:
        return sql, []
    bindings = []
    for statement in statements:
        if statement is None:
            continue
        for alias in statement.find_all(exp.Alias):
            name = columns.get(alias.alias)
            if name is None:
                continue
            literal = alias.this
            while isinstance(literal, exp.Cast):
                literal = literal.this
            if not isinstance(literal, exp.Literal) or not literal.is_string:
                raise ValueError("Runtime parameter column must contain a string literal: " + alias.alias)
            if "start" not in literal.meta or "end" not in literal.meta:
                raise ValueError("Runtime parameter has no source span: " + alias.alias)
            bindings.append({"name": name, "column": alias.alias, "value": literal.this,
                             "start": literal.meta["start"], "end": literal.meta["end"] + 1})
    template = sql
    for binding in sorted(bindings, key=lambda item: item["start"], reverse=True):
        template = template[:binding["start"]] + "{{dbt_runtime:" + binding["name"] + "}}" + template[binding["end"]:]
    return template, bindings


def comparison_node(node, columns, dialect):
    result = copy.deepcopy({key: value for key, value in node.items() if key not in {"fingerprint", "comparison_fingerprint", "runtime_parameters"}})
    result["compiled_sql"], _ = parameterize(result.get("compiled_sql"), columns, dialect)
    for statement in result.get("generated_statements", []):
        statement["sql"], _ = parameterize(statement["sql"], columns, dialect)
    return result
