from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "gpt-action-openapi.yaml"


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise AssertionError(f"duplicate YAML key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _walk(value: Any, path: str = "root"):
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, f"{path}/{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{path}/{index}")


def test_custom_gpt_action_schema_uses_explicit_objects_and_parameters() -> None:
    document = yaml.load(SCHEMA_PATH.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    schemas = document["components"]["schemas"]

    assert document["openapi"] == "3.1.0"
    assert document["info"]["version"] == "0.2.5"
    assert (
        document["info"]["x-potato-schema-build"]
        == "historical-submissions-3.1-20260808"
    )

    operation_ids: list[str] = []
    for route, path_item in document["paths"].items():
        placeholders = set(re.findall(r"\{([^}]+)\}", route))
        for method, operation in path_item.items():
            if not isinstance(operation, dict) or "operationId" not in operation:
                continue
            operation_ids.append(operation["operationId"])
            parameters = operation.get("parameters", [])
            assert all("$ref" not in parameter for parameter in parameters)
            path_names = {
                parameter.get("name")
                for parameter in parameters
                if parameter.get("in") == "path"
            }
            assert path_names == placeholders, (route, method, path_names, placeholders)

    assert len(operation_ids) == 28
    assert len(operation_ids) == len(set(operation_ids))

    for path, value in _walk(document):
        assert value is not None, f"null YAML node at {path}"
        if isinstance(value, dict) and value.get("type") == "object":
            assert value.get("properties"), f"object schema missing properties at {path}"
        if isinstance(value, dict) and value.get("type") == "array":
            assert isinstance(value.get("items"), dict), f"array schema missing items at {path}"
        if isinstance(value, dict) and "type" in value:
            assert isinstance(value["type"], str), f"union type is not Actions-safe at {path}"
        if isinstance(value, dict):
            assert "anyOf" not in value, f"anyOf is not Actions-safe at {path}"
            assert "oneOf" not in value, f"oneOf is not Actions-safe at {path}"
            assert "nullable" not in value, f"nullable is not OpenAPI 3.1 at {path}"
        if isinstance(value, dict) and "$ref" in value:
            reference = value["$ref"]
            if reference.startswith("#/components/schemas/"):
                assert reference.rsplit("/", 1)[-1] in schemas, f"unknown schema ref: {reference}"
