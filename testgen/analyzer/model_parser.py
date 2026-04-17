"""
model_parser.py — extracts field definitions from Java model/DTO classes.

Output shape:
{
  "UserRequest": [
    {"name": "name",  "type": "String",  "validations": ["@NotBlank", "@Size(min=2,max=100)"]},
    {"name": "email", "type": "String",  "validations": ["@NotBlank", "@Email"]},
    {"name": "age",   "type": "Integer", "validations": []},
    {"name": "city",  "type": "String",  "validations": []},
  ]
}
"""

import re

_FIELD_RE = re.compile(
    r'((?:@\w+(?:\([^)]*\))?\s*)+)?'   # zero or more annotations
    r'private\s+([\w<>]+)\s+(\w+)\s*;' # private Type name;
)

_ANNOTATION_RE = re.compile(r'@\w+(?:\([^)]*\))?')


def parse_models(models: dict) -> dict:
    """
    models: {"ClassName": "<java source>", ...}
    Returns: {"ClassName": [field dicts]}
    """
    result = {}
    for class_name, source in models.items():
        fields = []
        for m in _FIELD_RE.finditer(source):
            annotations_block = m.group(1) or ""
            field_type = m.group(2)
            field_name = m.group(3)
            validations = _ANNOTATION_RE.findall(annotations_block)
            fields.append({
                "name":        field_name,
                "type":        field_type,
                "validations": validations,
            })
        if fields:
            result[class_name] = fields
    return result


def models_to_text(parsed: dict) -> str:
    """Convert parsed models to compact text for LLM context."""
    lines = []
    for class_name, fields in parsed.items():
        lines.append(f"Model: {class_name}")
        for f in fields:
            v = ", ".join(f["validations"]) if f["validations"] else "no constraints"
            lines.append(f"  {f['name']}  {f['type']}  [{v}]")
        lines.append("")
    return "\n".join(lines)
