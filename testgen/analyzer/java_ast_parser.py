from __future__ import annotations

"""java_ast_parser.py — AST-based Java source parser using javalang.

Replaces regex-based endpoint and model parsing with accurate AST traversal.
Falls back to the original regex parsers automatically if:
  - javalang is not installed
  - The source file uses syntax javalang cannot handle (records, sealed classes, etc.)

Usage:
    from testgen.analyzer.java_ast_parser import parse_endpoints_ast, parse_models_ast

    endpoints = parse_endpoints_ast(controller_source)
    models    = parse_models_ast({"UserRequest": source, "DataResponse": source2})
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_MAPPING_ANNOTATIONS = {"GetMapping", "PostMapping", "PutMapping", "DeleteMapping"}
_HTTP_METHOD = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
}
_PARAM_ANNOTATIONS = {"PathVariable", "RequestParam", "RequestBody"}

# Javadoc extractor — AST does not carry doc comments
_JAVADOC_BEFORE_METHOD_RE = re.compile(
    r'/\*\*(.*?)\*/\s*(?:@\w+[^\n]*\n\s*)*'
    r'(?:public|private|protected)[^\n]*\s+(\w+)\s*\(',
    re.DOTALL,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_endpoints_ast(source: str) -> list[dict]:
    """Parse REST endpoints from a Spring Boot controller source using AST."""
    try:
        import javalang
    except ImportError:
        logger.debug("javalang not installed — using regex parser")
        return _fallback_endpoints(source)

    try:
        tree = javalang.parse.parse(source)
    except Exception as exc:
        logger.warning("AST parse failed (%s) — using regex parser", exc)
        return _fallback_endpoints(source)

    base_path = _get_base_path(tree)
    javadocs = _extract_all_javadocs(source)
    endpoints = []

    for _, cls in tree.filter(javalang.tree.ClassDeclaration):
        for method in cls.methods:
            ep = _extract_endpoint(method, base_path, javadocs)
            if ep:
                endpoints.append(ep)

    return endpoints


def parse_models_ast(models: dict[str, str]) -> dict[str, list[dict]]:
    """Parse field definitions from Java model/DTO class sources using AST.

    Args:
        models: {"ClassName": "<java source>", ...}

    Returns:
        {"ClassName": [{"name": ..., "type": ..., "validations": [...]}, ...], ...}
    """
    result = {}
    for class_name, source in models.items():
        fields = _parse_model_fields(class_name, source)
        if fields:
            result[class_name] = fields
    return result


# ---------------------------------------------------------------------------
# Endpoint extraction helpers
# ---------------------------------------------------------------------------


def _get_base_path(tree: Any) -> str:
    """Extract class-level @RequestMapping base path."""
    try:
        import javalang
        for _, cls in tree.filter(javalang.tree.ClassDeclaration):
            for ann in cls.annotations or []:
                if ann.name == "RequestMapping":
                    return _annotation_string_value(ann).rstrip("/")
    except Exception:
        pass
    return ""


def _extract_endpoint(method: Any, base_path: str, javadocs: dict[str, str]) -> dict | None:
    """Build an endpoint dict from a method node, or None if not a mapped method."""
    mapping_ann = None
    for ann in method.annotations or []:
        if ann.name in _MAPPING_ANNOTATIONS:
            mapping_ann = ann
            break

    if mapping_ann is None:
        return None

    http_method = _HTTP_METHOD[mapping_ann.name]
    suffix = _annotation_string_value(mapping_ann).strip("/")
    full_path = f"{base_path}/{suffix}".rstrip("/") if suffix else base_path

    params: list[dict] = []
    request_body: str | None = None

    for param in method.parameters or []:
        for ann in param.annotations or []:
            if ann.name == "RequestBody":
                request_body = _type_name(param.type)
            elif ann.name in ("PathVariable", "RequestParam"):
                params.append({
                    "name": param.name,
                    "type": _type_name(param.type),
                    "source": "path" if ann.name == "PathVariable" else "query",
                })

    return {
        "method": http_method,
        "path": full_path,
        "method_name": method.name,
        "description": javadocs.get(method.name, _camel_to_desc(method.name)),
        "params": params,
        "request_body": request_body,
    }


def _annotation_string_value(ann: Any) -> str:
    """Extract the string value from a mapping annotation element."""
    if ann.element is None:
        return ""

    elem = ann.element

    # Single literal: @GetMapping("/path")
    if hasattr(elem, "value") and isinstance(elem.value, str):
        return elem.value.strip("\"'")

    # List of ElementValuePairs: @GetMapping(value = "/path", ...)
    if isinstance(elem, list):
        for pair in elem:
            if hasattr(pair, "name") and pair.name in ("value", None):
                if hasattr(pair, "value") and hasattr(pair.value, "value"):
                    return str(pair.value.value).strip("\"'")
        # Fall through: try any pair with a string value
        for pair in elem:
            if hasattr(pair, "value") and hasattr(pair.value, "value"):
                return str(pair.value.value).strip("\"'")

    return ""


def _type_name(type_node: Any) -> str:
    """Get a human-readable type name, including generic arguments."""
    if type_node is None:
        return "Object"
    name = getattr(type_node, "name", "Object")
    args = getattr(type_node, "arguments", None)
    if args:
        try:
            inner = ", ".join(
                _type_name(a.type) for a in args if hasattr(a, "type") and a.type
            )
            return f"{name}<{inner}>" if inner else name
        except Exception:
            pass
    return name


# ---------------------------------------------------------------------------
# Model extraction helpers
# ---------------------------------------------------------------------------


def _parse_model_fields(class_name: str, source: str) -> list[dict]:
    """Extract fields from a model class using AST."""
    try:
        import javalang
    except ImportError:
        return _fallback_model_fields(class_name, source)

    try:
        tree = javalang.parse.parse(source)
    except Exception as exc:
        logger.warning("AST parse failed for %s (%s) — using regex parser", class_name, exc)
        return _fallback_model_fields(class_name, source)

    fields: list[dict] = []
    try:
        for _, cls in tree.filter(javalang.tree.ClassDeclaration):
            for field_decl in cls.fields or []:
                type_str = _type_name(field_decl.type)
                validations = [
                    _annotation_repr(ann)
                    for ann in (field_decl.annotations or [])
                ]
                for declarator in field_decl.declarators:
                    fields.append({
                        "name": declarator.name,
                        "type": type_str,
                        "validations": validations,
                    })
    except Exception as exc:
        logger.warning("AST field extraction failed for %s (%s) — using regex parser", class_name, exc)
        return _fallback_model_fields(class_name, source)

    return fields


def _annotation_repr(ann: Any) -> str:
    """Compact string representation of an annotation node."""
    if ann.element is None:
        return f"@{ann.name}"
    elem = ann.element
    if hasattr(elem, "value"):
        return f"@{ann.name}({elem.value})"
    if isinstance(elem, list):
        parts = []
        for pair in elem:
            if hasattr(pair, "name") and hasattr(pair, "value"):
                parts.append(f"{pair.name}={getattr(pair.value, 'value', '?')}")
        return f"@{ann.name}({', '.join(parts)})" if parts else f"@{ann.name}(...)"
    return f"@{ann.name}(...)"


# ---------------------------------------------------------------------------
# Javadoc extraction (regex — AST strips comments)
# ---------------------------------------------------------------------------


def _extract_all_javadocs(source: str) -> dict[str, str]:
    """Build a map of method_name -> first sentence of its Javadoc."""
    result: dict[str, str] = {}
    for m in _JAVADOC_BEFORE_METHOD_RE.finditer(source):
        doc_text = re.sub(r"\s*\*\s*", " ", m.group(1)).strip()
        method_name = m.group(2)
        sentence = doc_text.split(".")[0].strip()
        if sentence:
            result[method_name] = sentence
    return result


def _camel_to_desc(name: str) -> str:
    words = re.sub(r"([A-Z])", r" \1", name).strip().lower()
    return words.capitalize()


# ---------------------------------------------------------------------------
# Regex fallbacks (delegate to original parsers)
# ---------------------------------------------------------------------------


def _fallback_endpoints(source: str) -> list[dict]:
    from testgen.analyzer.endpoint_parser import _parse_endpoints_regex
    return _parse_endpoints_regex(source)


def _fallback_model_fields(class_name: str, source: str) -> list[dict]:
    from testgen.analyzer.model_parser import _parse_model_fields_regex
    result = _parse_model_fields_regex({class_name: source})
    return result.get(class_name, [])
