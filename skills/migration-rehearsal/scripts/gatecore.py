"""Rules shared by the sandbox (rehearse.py) and the gate (db_gate/server.py).

Everything that decides whether a migration is safe lives here, so the gate can
recompute a verdict from raw findings instead of trusting the label a report carries.
Pure Python + psycopg; no other dependencies.
"""
from __future__ import annotations

import hashlib
import json
import re

REPORT_VERSION = 1
LOCK_REVIEW_MS = 2000          # a write-blocking lock held longer than this needs a human look
SAMPLES_PER_COLUMN = 3

VERDICT_SAFE, VERDICT_REVIEW, VERDICT_BLOCK = "SAFE", "REVIEW", "BLOCK"

# Lock modes that block INSERT/UPDATE/DELETE from other sessions.
WRITE_BLOCKING_LOCKS = {"ShareLock", "ShareRowExclusiveLock", "ExclusiveLock", "AccessExclusiveLock"}


# ---------------------------------------------------------------- hashing

def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_sql(sql: str) -> str:
    """Line endings and surrounding whitespace don't change a migration's identity."""
    return sql.replace("\r\n", "\n").strip()


def sql_sha256(sql: str) -> str:
    return sha256_text(normalize_sql(sql))


def report_digest(report: dict) -> str:
    body = {k: v for k, v in report.items() if k != "digest"}
    return sha256_text(canonical_json(body))


# ---------------------------------------------------------------- SQL statements

def split_statements(sql: str) -> list[str]:
    """Split a script on top-level semicolons, respecting quotes, comments and $tag$ bodies."""
    out, buf, i, n = [], [], 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j == -1 else j
            buf.append(sql[i:j]); i = j
        elif c == "/" and sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            j = n if j == -1 else j + 2
            buf.append(sql[i:j]); i = j
        elif c in ("'", '"'):
            j = i + 1
            while j < n:
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            buf.append(sql[i:j + 1]); i = j + 1
        elif c == "$":
            m = re.match(r"\$[A-Za-z_]?[A-Za-z0-9_]*\$", sql[i:])
            if m:
                tag = m.group(0)
                j = sql.find(tag, i + len(tag))
                j = n if j == -1 else j + len(tag)
                buf.append(sql[i:j]); i = j
            else:
                buf.append(c); i += 1
        elif c == ";":
            stmt = "".join(buf).strip()
            if strip_comments(stmt).strip():
                out.append(stmt)
            buf = []; i += 1
        else:
            buf.append(c); i += 1
    stmt = "".join(buf).strip()
    if strip_comments(stmt).strip():
        out.append(stmt)
    return out


def strip_comments(stmt: str) -> str:
    stmt = re.sub(r"/\*.*?\*/", " ", stmt, flags=re.S)
    return re.sub(r"--[^\n]*", " ", stmt)


_NON_TX = [
    (re.compile(r"\bCONCURRENTLY\b", re.I), "CONCURRENTLY"),
    (re.compile(r"^\s*VACUUM\b", re.I), "VACUUM"),
    (re.compile(r"^\s*(CREATE|DROP|ALTER)\s+DATABASE\b", re.I), "DATABASE DDL"),
    (re.compile(r"^\s*(CREATE|DROP)\s+TABLESPACE\b", re.I), "TABLESPACE DDL"),
    (re.compile(r"^\s*ALTER\s+SYSTEM\b", re.I), "ALTER SYSTEM"),
    (re.compile(r"^\s*REINDEX\s+(SYSTEM|DATABASE)\b", re.I), "REINDEX SYSTEM/DATABASE"),
    (re.compile(r"^\s*(BEGIN|START\s+TRANSACTION|COMMIT|END|ROLLBACK|ABORT|SAVEPOINT|RELEASE|PREPARE\s+TRANSACTION)\b", re.I),
     "transaction control"),
]


def non_transactional_statements(sql: str) -> list[str]:
    """Statements that can't run inside the single transaction every migration is wrapped in."""
    found = []
    for idx, stmt in enumerate(split_statements(sql), 1):
        body = strip_comments(stmt)
        for rx, label in _NON_TX:
            if rx.search(body):
                found.append(f"#{idx} {label}: {' '.join(body.split())[:80]}")
                break
    return found


# ---------------------------------------------------------------- schema fingerprint

_FP_QUERIES = {
    # pg_catalog rather than information_schema: the latter hides objects the role can't read.
    "columns": """
        SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
               coalesce(pg_get_expr(d.adbin, d.adrelid), '')
          FROM pg_attribute a
          JOIN pg_class c ON c.oid = a.attrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
          LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
         WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND a.attnum > 0 AND NOT a.attisdropped
         ORDER BY c.relname, a.attnum""",
    "indexes": """
        SELECT ic.relname, pg_get_indexdef(i.indexrelid)
          FROM pg_index i
          JOIN pg_class ic ON ic.oid = i.indexrelid
          JOIN pg_namespace n ON n.oid = ic.relnamespace
         WHERE n.nspname = 'public'
         ORDER BY 1""",
    "constraints": """
        SELECT cl.relname, co.conname, pg_get_constraintdef(co.oid)
          FROM pg_constraint co
          JOIN pg_class cl ON cl.oid = co.conrelid
          JOIN pg_namespace n ON n.oid = co.connamespace
         WHERE n.nspname = 'public'
         ORDER BY 1, 2""",
    "views": """
        SELECT c.relname, c.relkind, pg_get_viewdef(c.oid)
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind IN ('v', 'm')
         ORDER BY 1""",
    "sequences": """
        SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind = 'S' ORDER BY 1""",
    "triggers": """
        SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid)
          FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND NOT t.tgisinternal
         ORDER BY 1, 2""",
}


def schema_description(conn) -> dict:
    desc = {}
    with conn.cursor() as cur:
        for key, q in _FP_QUERIES.items():
            cur.execute(q)
            desc[key] = [[str(v) for v in row] for row in cur.fetchall()]
    return desc


def fingerprint(conn) -> str:
    """sha256 over the public schema's shape (columns, types, defaults, indexes, constraints...)."""
    return sha256_text(canonical_json(schema_description(conn)))


# ---------------------------------------------------------------- verdict

def compute_verdict(findings: dict) -> tuple[str, list[str]]:
    """The only place a verdict is decided. Returns (verdict, reasons)."""
    block, review = [], []
    mig = findings.get("migration", {})
    if not mig.get("ok"):
        block.append("migration failed")
    if findings.get("non_transactional"):
        block.append("non-transactional statements")
    for name, t in sorted(findings.get("tables", {}).items()):
        if t.get("status") == "dropped" and t.get("rows_before", 0) > 0:
            block.append(f"{name} dropped with {t['rows_before']} rows")
            continue
        if t.get("rows_deleted", 0) > 0:
            block.append(f"{name}: {t['rows_deleted']} rows deleted")
        for col, n in sorted(t.get("columns_dropped", {}).items()):
            if n > 0:
                block.append(f"{name}.{col} dropped with {n} non-null values")
        for col, n in sorted(t.get("columns_changed", {}).items()):
            if n > 0:
                review.append(f"{name}.{col} changed on {n} rows")
        if t.get("status") != "new" and t.get("rows_inserted", 0) > 0:
            review.append(f"{name}: {t['rows_inserted']} rows inserted")
        if t.get("no_primary_key") and t.get("rows_before") != t.get("rows_after"):
            review.append(f"{name}: row count changed and table has no primary key to diff")
    lock_ms = findings.get("locks", {}).get("max_write_lock_ms", 0)
    if lock_ms > LOCK_REVIEW_MS:
        review.append(f"write lock held {lock_ms} ms (> {LOCK_REVIEW_MS} ms)")
    if block:
        return VERDICT_BLOCK, block + review
    if review:
        return VERDICT_REVIEW, review
    return VERDICT_SAFE, []


def summarize(findings: dict, verdict: str) -> str:
    """One deterministic impact line. The gate recomputes it; apply_migration must quote it verbatim."""
    parts = []
    mig = findings.get("migration", {})
    if not mig.get("ok"):
        err = (mig.get("error") or "error").splitlines()[0][:160]
        parts.append(f"migration failed at statement {mig.get('failed_statement')}: {err}")
    if findings.get("non_transactional"):
        parts.append("non-transactional: " + ", ".join(findings["non_transactional"]))
    tables = findings.get("tables", {})
    touched = any(t.get("rows_deleted") or t.get("rows_modified") or
                  (t.get("rows_inserted") and t.get("status") != "new") or t.get("status") == "dropped"
                  for t in tables.values())
    if mig.get("ok") and not touched:
        parts.append("no existing rows modified")
    for name, t in sorted(tables.items()):
        if t.get("status") == "new":
            parts.append(f"new table {name} ({t.get('rows_after', 0):,} rows)")
            continue
        if t.get("status") == "dropped":
            parts.append(f"{name} dropped ({t.get('rows_before', 0):,} rows)")
            continue
        bits = []
        if t.get("rows_deleted"):
            bits.append(f"{t['rows_deleted']:,} rows deleted")
        if t.get("rows_inserted"):
            bits.append(f"{t['rows_inserted']:,} rows inserted")
        for col, n in sorted(t.get("columns_changed", {}).items()):
            bits.append(f"{col} rewritten on {n:,}/{t.get('rows_before', 0):,} rows")
        for col, n in sorted(t.get("columns_added", {}).items()):
            bits.append(f"+{col} ({n:,}/{t.get('rows_after', 0):,} populated)")
        for col in sorted(t.get("columns_dropped", {})):
            bits.append(f"-{col}")
        if bits:
            parts.append(f"{name}: " + ", ".join(bits))
    locks = findings.get("locks", {})
    if locks.get("max_write_lock_ms") is not None and locks.get("tables"):
        worst = max(locks["tables"].items(), key=lambda kv: kv[1]["held_ms"])
        parts.append(f"max write lock {locks['max_write_lock_ms']:,} ms on {worst[0]} ({worst[1]['mode']})")
    return f"{verdict}: " + "; ".join(parts)
