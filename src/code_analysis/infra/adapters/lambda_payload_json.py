"""Serialización JSON para invocaciones Lambda (dataclasses → dict recursivo)."""

import json
from dataclasses import asdict, is_dataclass
from typing import Any, Mapping


def dumps_lambda_payload(payload: Mapping[str, Any]) -> bytes:
    """Serializa un dict de payload; convierte dataclasses con el hook `default`."""

    def default(obj: Any) -> Any:
        if is_dataclass(obj) and not isinstance(obj, type):
            return asdict(obj)
        raise TypeError(
            f"Object of type {obj.__class__.__name__} is not JSON serializable"
        )

    return bytes(json.dumps(payload, default=default), "utf-8")
