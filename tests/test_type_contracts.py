"""Keep the issue's type/HTTP coverage acceptance budgets in normal verification."""

import ast
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_standard_type_checking_and_parameter_budget():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert config["tool"]["pyright"]["typeCheckingMode"] == "standard"
    missing = []
    for path in (ROOT / "app").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            arguments = [
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
                *([node.args.vararg] if node.args.vararg else []),
                *([node.args.kwarg] if node.args.kwarg else []),
            ]
            missing.extend(
                f"{path.relative_to(ROOT)}:{node.name}.{argument.arg}"
                for argument in arguments
                if argument.annotation is None and argument.arg not in {"self", "cls"}
            )
    assert len(missing) <= 60, missing


def test_at_least_eighty_percent_of_http_routes_have_explicit_response_models():
    routes = []
    for path in (ROOT / "app/api").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for decorator in node.decorator_list:
                if not (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and isinstance(decorator.func.value, ast.Name)
                    and decorator.func.value.id in {"router", "coverage_router"}
                    and decorator.func.attr
                    in {
                        "get",
                        "head",
                        "post",
                        "put",
                        "patch",
                        "delete",
                        "options",
                        "api_route",
                    }
                ):
                    continue
                routes.append(
                    any(
                        keyword.arg == "response_model"
                        and not (
                            isinstance(keyword.value, ast.Constant) and keyword.value.value is None
                        )
                        for keyword in decorator.keywords
                    )
                )
    assert routes and sum(routes) / len(routes) >= 0.8
