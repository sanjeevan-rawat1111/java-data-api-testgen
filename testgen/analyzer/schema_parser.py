"""
schema_parser.py — extracts table definitions from schema.sql.

Output shape:
{
  "users": {
    "columns": [
      {"name": "id",    "type": "BIGINT",    "nullable": False, "extra": "AUTO_INCREMENT PRIMARY KEY"},
      {"name": "name",  "type": "VARCHAR",   "nullable": False, "extra": ""},
      {"name": "email", "type": "VARCHAR",   "nullable": False, "extra": "UNIQUE"},
      ...
    ]
  }
}
"""

import re


_CREATE_TABLE_RE = re.compile(
    r'CREATE TABLE(?:\s+IF NOT EXISTS)?\s+[`"]?(\w+)[`"]?\s*\((.*?)\)\s*;',
    re.DOTALL | re.IGNORECASE,
)

_COLUMN_RE = re.compile(
    r'^\s*[`"]?(\w+)[`"]?\s+([\w()]+(?:\s*\(\s*\d+(?:\s*,\s*\d+)?\s*\))?)'
    r'(.*?)(?:,\s*$|$)',
    re.IGNORECASE,
)

_SKIP_PREFIXES = ("PRIMARY", "UNIQUE", "INDEX", "KEY", "CONSTRAINT", "CHECK", "FOREIGN")


def parse_schema(sql: str) -> dict:
    tables = {}
    for match in _CREATE_TABLE_RE.finditer(sql):
        table_name = match.group(1).lower()
        body       = match.group(2)
        columns    = []

        for line in body.splitlines():
            line = line.strip().rstrip(",")
            if not line:
                continue
            if any(line.upper().startswith(p) for p in _SKIP_PREFIXES):
                continue

            m = _COLUMN_RE.match(line)
            if not m:
                continue

            col_name = m.group(1)
            col_type = m.group(2).upper()
            extras   = m.group(3).strip().upper() if m.group(3) else ""

            nullable = "NOT NULL" not in extras

            columns.append({
                "name":     col_name,
                "type":     col_type,
                "nullable": nullable,
                "extra":    extras,
            })

        if columns:
            tables[table_name] = {"columns": columns}

    return tables


def schema_to_text(parsed: dict) -> str:
    """Convert parsed schema to a compact human-readable format for LLM context."""
    lines = []
    for table, info in parsed.items():
        lines.append(f"Table: {table}")
        for col in info["columns"]:
            nullable = "nullable" if col["nullable"] else "NOT NULL"
            lines.append(f"  {col['name']}  {col['type']}  {nullable}  {col['extra']}".rstrip())
        lines.append("")
    return "\n".join(lines)
