"""
endpoint_parser.py — extracts REST endpoint metadata from Java controller source.

For each controller method it extracts:
  {
    "method":      "GET" | "POST" | "PUT" | "DELETE",
    "path":        "/data/health",
    "description": "Check database health status",
    "params":      [{"name": "id", "type": "Long", "source": "path|body|query"}],
    "request_body": "UserRequest" | None,
    "response":    "DataResponse",
  }
"""

import re


# Maps Spring annotation → HTTP method
_MAPPING = {
    "GetMapping":    "GET",
    "PostMapping":   "POST",
    "PutMapping":    "PUT",
    "DeleteMapping": "DELETE",
}

# Matches method-level mapping annotations only (NOT RequestMapping)
_MAPPING_RE = re.compile(
    r'@(GetMapping|PostMapping|PutMapping|DeleteMapping)'
    r'(?:\([^)]*?(?:value\s*=\s*)?["\']([^"\']*)["\'][^)]*\)|\("([^"]*)"\)|\(\'([^\']*)\'\)|\(\))?'
)

_BASE_PATH_RE = re.compile(
    r'@RequestMapping\(["\']([^"\']+)["\']'
)

_METHOD_SIG_RE = re.compile(
    r'(?:public|private|protected)\s+'
    r'(?:ResponseEntity<[^>]+>|DataResponse|String|void|[\w<>]+)\s+'
    r'(\w+)\s*\(([^)]*)\)'
)

_PARAM_RE = re.compile(
    r'@(PathVariable|RequestBody|RequestParam)[^)]*\)?\s+'
    r'(?:[\w<>\.]+\s+)?'
    r'([\w<>\.]+)\s+(\w+)'
)

_JAVADOC_RE = re.compile(r'/\*\*(.*?)\*/', re.DOTALL)


def _extract_base_path(source: str) -> str:
    m = _BASE_PATH_RE.search(source)
    return m.group(1).rstrip("/") if m else ""


def _extract_last_javadoc(block: str) -> str:
    """
    Return the LAST Javadoc comment in a block.

    Why last: when we split on @Mapping annotations, the Javadoc that belongs
    to method N ends up at the END of chunk N-1 (it sits just before the annotation
    in the source but the split happens at the annotation, not at the /**).
    """
    matches = list(_JAVADOC_RE.finditer(block))
    if not matches:
        return ""
    text = re.sub(r'\s*\*\s*', ' ', matches[-1].group(1)).strip()
    # Take first sentence only
    return text.split(".")[0].strip()


def _source_for(annotation: str) -> str:
    return {"PathVariable": "path", "RequestBody": "body", "RequestParam": "query"}.get(
        annotation, "unknown"
    )


def parse_endpoints(controller_content: str) -> list[dict]:
    base_path = _extract_base_path(controller_content)
    endpoints = []

    # Split BEFORE each method-level mapping annotation.
    # NOTE: @RequestMapping is intentionally excluded — it is class-level
    # and its base path is already captured by _extract_base_path().
    chunks = re.split(r'(?=@(?:Get|Post|Put|Delete)Mapping)', controller_content)

    for i, chunk in enumerate(chunks):
        m = _MAPPING_RE.search(chunk)
        if not m:
            continue

        annotation = m.group(1)
        path_suffix = next((g for g in m.groups()[1:] if g is not None), "")
        full_path = (base_path + "/" + path_suffix.lstrip("/")).rstrip("/") if path_suffix \
            else base_path

        http_method = _MAPPING[annotation]

        # Extract method signature
        sig = _METHOD_SIG_RE.search(chunk)
        method_name = sig.group(1) if sig else "unknown"

        # Extract parameters from method signature area only
        params = []
        request_body = None
        for pm in _PARAM_RE.finditer(chunk[:600]):
            ann, ptype, pname = pm.group(1), pm.group(2), pm.group(3)
            if ann == "RequestBody":
                request_body = ptype
            else:
                params.append({
                    "name":   pname,
                    "type":   ptype,
                    "source": _source_for(ann),
                })

        # FIX: the Javadoc for THIS method is in the PREVIOUS chunk.
        # The split happens at "@GetMapping" but the "/** ... */" comment
        # sits immediately BEFORE that annotation in the source — so it
        # ends up at the tail of the previous chunk after the split.
        prev_chunk = chunks[i - 1] if i > 0 else ""
        description = _extract_last_javadoc(prev_chunk) or _method_name_to_desc(method_name)

        endpoints.append({
            "method":       http_method,
            "path":         full_path,
            "method_name":  method_name,
            "description":  description,
            "params":       params,
            "request_body": request_body,
        })

    return endpoints


def _method_name_to_desc(name: str) -> str:
    """Convert camelCase method name to a readable description."""
    words = re.sub(r'([A-Z])', r' \1', name).strip().lower()
    return words.capitalize()
