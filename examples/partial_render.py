"""Server-side partial rendering of startup graphs; never executes project code."""

import argparse
import json
import operator
from pathlib import Path

from jinja2 import Environment, TemplateSyntaxError, nodes


class Unresolved(Exception):
    pass


OPERATORS = {nodes.Add: operator.add, nodes.Sub: operator.sub, nodes.Mul: operator.mul,
             nodes.Div: operator.truediv, nodes.FloorDiv: operator.floordiv, nodes.Mod: operator.mod}
COMPARE = {"eq": operator.eq, "ne": operator.ne, "gt": operator.gt, "gteq": operator.ge,
           "lt": operator.lt, "lteq": operator.le, "in": lambda a, b: a in b,
           "notin": lambda a, b: a not in b}


class PartialRenderer:
    def __init__(self, graph, node_id):
        self.graph, self.node = graph, graph["nodes"][node_id]
        self.remaining = 10000
        self.context = {"execute": True, "invocation_id": graph["invocation_id"],
                        "project_name": graph["project_name"], "target": graph.get("target", {})}
        if self.node.get("relation_name") and self.node.get("materialized") != "ephemeral":
            self.context["this"] = self.node["relation_name"]

    def expression(self, expr, context):
        self.remaining -= 1
        if self.remaining < 0:
            raise Unresolved("render_budget_exceeded")
        if isinstance(expr, nodes.Const):
            return expr.value
        if isinstance(expr, nodes.Name):
            if expr.name not in context or isinstance(context[expr.name], Unresolved):
                raise Unresolved("unknown_value:" + expr.name)
            return context[expr.name]
        if isinstance(expr, (nodes.Tuple, nodes.List)):
            return [self.expression(item, context) for item in expr.items]
        if isinstance(expr, nodes.Dict):
            return {self.expression(pair.key, context): self.expression(pair.value, context) for pair in expr.items}
        if isinstance(expr, nodes.Getattr):
            value = self.expression(expr.node, context)
            if isinstance(value, dict) and expr.attr in value:
                return value[expr.attr]
            raise Unresolved("unknown_attribute:" + expr.attr)
        if isinstance(expr, nodes.Getitem):
            value, key = self.expression(expr.node, context), self.expression(expr.arg, context)
            if not isinstance(value, (dict, list, tuple, str)):
                raise Unresolved("unsupported_index")
            return value[key]
        if isinstance(expr, nodes.Call):
            if not isinstance(expr.node, nodes.Name) or expr.dyn_args or expr.dyn_kwargs:
                raise Unresolved("unsupported_call")
            name = expr.node.name
            if name not in {"ref", "source", "config"}:
                raise Unresolved("unresolved_call:" + name)
            args = [self.expression(arg, context) for arg in expr.args]
            kwargs = {kw.key: self.expression(kw.value, context) for kw in expr.kwargs}
            if name == "config":
                return ""
            candidates = [self.graph["nodes"][key] for key in self.node["depends_on"] if key in self.graph["nodes"]]
            if name == "ref" and len(args) in {1, 2}:
                candidates = [n for n in candidates if n.get("name") == args[-1] and n["resource_type"] in {"model", "seed", "snapshot"} and (len(args) == 1 or n.get("package_name") == args[0])]
                version = kwargs.get("version", kwargs.get("v"))
                if version is not None:
                    candidates = [n for n in candidates if str(n.get("version")) == str(version)]
                if kwargs.keys() - {"version", "v"}:
                    raise Unresolved("unsupported_ref_options")
            elif name == "source" and len(args) == 2 and not kwargs:
                candidates = [n for n in candidates if n["resource_type"] == "source" and n.get("source_name") == args[0] and n.get("name") == args[1]]
            else:
                raise Unresolved("unsupported_relation_call")
            if len(candidates) != 1 or not candidates[0].get("relation_name"):
                raise Unresolved("relation_resolution_unavailable")
            if candidates[0].get("materialized") == "ephemeral":
                raise Unresolved("ephemeral_requires_cte_injection")
            if candidates[0].get("defer_relation"):
                raise Unresolved("deferred_relation_requires_resolution")
            return candidates[0]["relation_name"]
        if type(expr) in OPERATORS:
            left, right = self.expression(expr.left, context), self.expression(expr.right, context)
            if isinstance(expr, nodes.Mul) and (isinstance(left, (str, list)) or isinstance(right, (str, list))):
                raise Unresolved("sequence_multiplication_not_supported")
            return OPERATORS[type(expr)](left, right)
        if isinstance(expr, nodes.Filter):
            value = self.expression(expr.node, context)
            args = [self.expression(arg, context) for arg in expr.args]
            if expr.kwargs or expr.dyn_args or expr.dyn_kwargs:
                raise Unresolved("unsupported_filter_arguments")
            if expr.name == "length" and not args:
                return len(value)
            if expr.name in {"upper", "lower", "trim"} and isinstance(value, str) and not args:
                return {"upper": str.upper, "lower": str.lower, "trim": str.strip}[expr.name](value)
            if expr.name == "join" and isinstance(value, (list, tuple)) and len(args) <= 1:
                return (str(args[0]) if args else "").join(str(item) for item in value)
            raise Unresolved("unsupported_filter:" + expr.name)
        if isinstance(expr, nodes.Concat):
            return "".join(str(self.expression(child, context)) for child in expr.nodes)
        if isinstance(expr, (nodes.Neg, nodes.Pos, nodes.Not)):
            value = self.expression(expr.node, context)
            return not value if isinstance(expr, nodes.Not) else -value if isinstance(expr, nodes.Neg) else +value
        if isinstance(expr, (nodes.And, nodes.Or)):
            left = self.expression(expr.left, context)
            if isinstance(expr, nodes.And):
                return self.expression(expr.right, context) if left else left
            return left if left else self.expression(expr.right, context)
        if isinstance(expr, nodes.Compare):
            left = self.expression(expr.expr, context)
            for operand in expr.ops:
                right = self.expression(operand.expr, context)
                if not COMPARE[operand.op](left, right):
                    return False
                left = right
            return True
        raise Unresolved("unsupported_expression:" + type(expr).__name__)

    def render_body(self, body, context):
        fragments = []
        for item in body:
            self.remaining -= 1
            if self.remaining < 0:
                fragments.append({"kind": "unresolved", "line": item.lineno, "reason": "render_budget_exceeded"})
                break
            try:
                if isinstance(item, nodes.Output):
                    for expr in item.nodes:
                        try:
                            text = expr.data if isinstance(expr, nodes.TemplateData) else str(self.expression(expr, context))
                            fragments.append({"kind": "text", "text": text})
                        except (Unresolved, TypeError, KeyError, IndexError, ZeroDivisionError) as error:
                            fragments.append({"kind": "unresolved", "line": expr.lineno, "reason": str(error)})
                elif isinstance(item, nodes.Assign) and isinstance(item.target, nodes.Name):
                    context[item.target.name] = self.expression(item.node, context)
                elif isinstance(item, nodes.If):
                    branches = [(item.test, item.body), *((branch.test, branch.body) for branch in item.elif_)]
                    selected = item.else_
                    for test, branch in branches:
                        if self.expression(test, context):
                            selected = branch
                            break
                    fragments.extend(self.render_body(selected, context))
                elif isinstance(item, nodes.For) and isinstance(item.target, nodes.Name) and not item.test and not item.recursive:
                    values = self.expression(item.iter, context)
                    if not isinstance(values, (list, tuple)) or len(values) > 1000:
                        raise Unresolved("unsupported_loop_iterable")
                    if not values:
                        fragments.extend(self.render_body(item.else_, context.copy()))
                    for index, value in enumerate(values):
                        loop_context = {**context, item.target.name: value, "loop": {"index": index + 1, "index0": index, "first": index == 0, "last": index == len(values) - 1, "length": len(values)}}
                        fragments.extend(self.render_body(item.body, loop_context))
                else:
                    raise Unresolved("unsupported_statement:" + type(item).__name__)
            except (Unresolved, TypeError, KeyError, IndexError, ZeroDivisionError) as error:
                fragments.append({"kind": "unresolved", "line": item.lineno, "reason": str(error)})
                assignments = ([item] if isinstance(item, nodes.Assign) else []) + list(item.find_all(nodes.Assign))
                for assignment in assignments:
                    for name in ([assignment.target] if isinstance(assignment.target, nodes.Name) else assignment.target.find_all(nodes.Name)):
                        context[name.name] = Unresolved(str(error))
                if isinstance(item, nodes.Macro):
                    context[item.name] = Unresolved("macro_not_evaluated")
        return fragments

    def render(self):
        raw = self.node.get("raw_code")
        if self.node.get("language", "sql") != "sql" or not raw:
            return {"status": "unavailable", "reason": "non_sql_or_source_unavailable"}
        if self.node.get("compiled_code"):
            return {"status": "observed_compiled_body", "sql": self.node["compiled_code"], "execution_authorized": False}
        try:
            ast = Environment(extensions=["jinja2.ext.do"]).parse(raw)
        except TemplateSyntaxError as error:
            return {"status": "unavailable", "reason": "unsupported_template_syntax", "line": error.lineno, "raw_code": raw, "execution_authorized": False}
        fragments = self.render_body(ast.body, dict(self.context))
        complete = all(fragment["kind"] == "text" for fragment in fragments)
        return {"status": "candidate_body" if complete else "partial_body", "renderer": "restricted_jinja_subset",
                "sql": "".join(fragment["text"] for fragment in fragments) if complete else None,
                "fragments": fragments, "raw_code": raw, "execution_authorized": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("graph", type=Path)
    args = parser.parse_args()
    graph = json.loads(args.graph.read_text())
    graph = graph.get("dbt_manifest", graph)
    print(json.dumps({key: PartialRenderer(graph, key).render() for key in graph["nodes"]}, indent=2))
