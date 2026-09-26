#!/usr/bin/env python3
"""Rehearse a Postgres migration against a production snapshot, inside the sandbox.

  rehearse.py run   --snapshot <file> --migration <file.sql> [--name NAME] [--out report.json]
  rehearse.py query [--db before|after] "SELECT ..."

`run` restores the snapshot twice (databases `before` and `after`), runs the migration on
`after` in one transaction while watching the locks it takes, diffs every row of every
table, and writes a report the gate can verify. Both databases stay up for `query`.

No production credentials exist here: the snapshot arrives as a file.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gatecore  # noqa: E402

HOME = Path(os.environ.get("REHEARSAL_HOME", "/tmp/rehearsal"))
PGDATA = HOME / "pgdata"
SNAPSHOT_MAGIC = "RELEASE-GATE-SNAPSHOT v1 "


def die(msg: str, code: int = 2):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------- postgres (bundled)

def server():
    try:
        import pgserver
    except ImportError:
        die("pgserver is not installed; run setup_sandbox.sh first")
    HOME.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:  # pgserver runs postgres as a separate 'pgserver' user when root
        os.chmod(HOME, 0o777)
    return pgserver.get_server(PGDATA, cleanup_mode=None)  # keep running for later `query` calls


def pg_bin(name: str) -> str:
    import pgserver
    return str(Path(pgserver.__file__).parent / "pginstall" / "bin" / name)


def connect(srv, db: str, autocommit=True):
    import psycopg
    return psycopg.connect(srv.get_uri(db), autocommit=autocommit)


# ---------------------------------------------------------------- snapshot

def load_snapshot(path: Path) -> tuple[dict, str]:
    raw = path.read_text()
    if raw.lstrip().startswith("{"):  # a harness that wrapped the tool result as JSON
        obj = json.loads(raw)
        raw = obj.get("result") or obj.get("text") or ""
    raw = raw.strip()
    if not raw.startswith(SNAPSHOT_MAGIC):
        die(f"{path} is not an export_snapshot result (missing '{SNAPSHOT_MAGIC.strip()}' header)")
    header_line, _, payload = raw.partition("\n")
    header = json.loads(header_line[len(SNAPSHOT_MAGIC):])
    dump_bytes = gzip.decompress(base64.b64decode("".join(payload.split())))
    if hashlib.sha256(dump_bytes).hexdigest() != header["dump_sha256"]:
        die("snapshot dump sha256 does not match its header (truncated or altered file)")
    return header, dump_bytes.decode("utf-8")


def sanitize_dump(dump: str) -> str:
    # Newer pg_dump emits psql meta-commands / settings the bundled PG16 psql doesn't know.
    keep = []
    for line in dump.splitlines():
        if line.startswith("\\restrict") or line.startswith("\\unrestrict"):
            continue
        if re.match(r"SET (transaction_timeout)\b", line):
            continue
        if line == "CREATE SCHEMA public;":  # `-n public` dumps include it; a fresh database already has it
            line = "CREATE SCHEMA IF NOT EXISTS public;"
        keep.append(line)
    return "\n".join(keep) + "\n"


def restore(srv, dump: str):
    with connect(srv, "postgres") as c:
        for db in ("after", "before"):
            c.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        c.execute("CREATE DATABASE before")
    dump_file = HOME / "snapshot.sql"
    dump_file.write_text(sanitize_dump(dump))
    os.chmod(dump_file, 0o644)
    t0 = time.monotonic()
    p = subprocess.run([pg_bin("psql"), "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", srv.get_uri("before"),
                        "-f", str(dump_file)], capture_output=True, text=True)
    if p.returncode != 0:
        die(f"restore failed:\n{p.stderr[-2000:]}")
    with connect(srv, "postgres") as c:
        c.execute("CREATE DATABASE after TEMPLATE before")
    return int((time.monotonic() - t0) * 1000)


# ---------------------------------------------------------------- migration run

LOCKS_Q = """
SELECT c.relname, l.mode
  FROM pg_locks l JOIN pg_class c ON c.oid = l.relation
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE l.pid = pg_backend_pid() AND l.locktype = 'relation' AND l.granted
   AND n.nspname = 'public' AND c.relkind IN ('r', 'p')
"""
LOCK_RANK = ["AccessShareLock", "RowShareLock", "RowExclusiveLock", "ShareUpdateExclusiveLock",
             "ShareLock", "ShareRowExclusiveLock", "ExclusiveLock", "AccessExclusiveLock"]


def run_migration(srv, sql: str) -> tuple[dict, dict, list]:
    statements = gatecore.split_statements(sql)
    first_blocking: dict[str, tuple[float, str]] = {}
    strongest: dict[str, str] = {}
    stmt_log = []
    mig = {"ok": True, "error": None, "sqlstate": None, "failed_statement": None, "statements": len(statements)}
    conn = connect(srv, "after", autocommit=False)
    t_start = time.monotonic()
    try:
        with conn.cursor() as cur:
            for i, stmt in enumerate(statements, 1):
                t0 = time.monotonic()
                try:
                    cur.execute(stmt)
                except Exception as e:  # noqa: BLE001 - report any database error
                    diag = getattr(e, "diag", None)
                    detail = getattr(diag, "message_detail", None) if diag else None
                    err = " ".join(str(e).split()) + (f" ({detail})" if detail and detail not in str(e) else "")
                    mig.update(ok=False, error=err, sqlstate=getattr(e, "sqlstate", None),
                               failed_statement=i)
                    stmt_log.append({"n": i, "sql": " ".join(gatecore.strip_comments(stmt).split())[:90],
                                     "ms": int((time.monotonic() - t0) * 1000), "rows": None, "error": True})
                    break
                ms = int((time.monotonic() - t0) * 1000)
                stmt_log.append({"n": i, "sql": " ".join(gatecore.strip_comments(stmt).split())[:90],
                                 "ms": ms, "rows": cur.rowcount if cur.rowcount >= 0 else None})
                cur.execute(LOCKS_Q)
                for rel, mode in cur.fetchall():
                    if LOCK_RANK.index(mode) > LOCK_RANK.index(strongest.get(rel, "AccessShareLock")):
                        strongest[rel] = mode
                    if mode in gatecore.WRITE_BLOCKING_LOCKS and rel not in first_blocking:
                        # held from the start of the statement that took it until commit
                        first_blocking[rel] = (t0, mode)
        if mig["ok"]:
            conn.commit()
        else:
            conn.rollback()
    finally:
        t_end = time.monotonic()
        conn.close()
    mig["duration_ms"] = int((t_end - t_start) * 1000)
    lock_tables = {rel: {"mode": strongest.get(rel, mode), "held_ms": int((t_end - t0) * 1000)}
                   for rel, (t0, mode) in first_blocking.items()}
    locks = {"max_write_lock_ms": max((v["held_ms"] for v in lock_tables.values()), default=0),
             "tables": lock_tables}
    return mig, locks, stmt_log


# ---------------------------------------------------------------- row diff

def table_meta(conn) -> dict:
    q = """
    SELECT c.relname,
           array(SELECT a.attname FROM pg_attribute a
                  WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum),
           array(SELECT a.attname FROM pg_index i
                  JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                  WHERE i.indrelid = c.oid AND i.indisprimary ORDER BY a.attnum)
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')"""
    return {name: {"columns": list(cols), "pk": list(pk)} for name, cols, pk in conn.execute(q).fetchall()}


def qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def fetch_rows(conn, table: str, pk: list[str], cols: list[str]) -> dict:
    sel = ", ".join([f"{qi(c)}::text" for c in pk] + [f"{qi(c)}::text" for c in cols])
    rows = {}
    with conn.cursor(name=f"diff_{table}") as cur:
        cur.itersize = 20000
        cur.execute(f"SELECT {sel} FROM {qi(table)}")
        for r in cur:
            rows[tuple(r[:len(pk)])] = r[len(pk):]
    return rows


def count(conn, table: str, col: str | None = None) -> int:
    expr = f"count({qi(col)})" if col else "count(*)"
    return conn.execute(f"SELECT {expr} FROM {qi(table)}").fetchone()[0]


def diff(srv) -> dict:
    b, a = connect(srv, "before"), connect(srv, "after")
    b.autocommit = a.autocommit = False  # named cursors need a transaction
    try:
        mb, ma = table_meta(b), table_meta(a)
        out = {}
        for t in sorted(set(mb) | set(ma)):
            if t not in ma:
                out[t] = {"status": "dropped", "rows_before": count(b, t), "rows_after": 0}
                continue
            if t not in mb:
                out[t] = {"status": "new", "rows_before": 0, "rows_after": count(a, t)}
                continue
            cb, ca = mb[t]["columns"], ma[t]["columns"]
            common = [c for c in cb if c in ca]
            added = [c for c in ca if c not in cb]
            dropped = [c for c in cb if c not in ca]
            info = {"status": "unchanged", "rows_before": count(b, t), "rows_after": count(a, t),
                    "rows_deleted": 0, "rows_inserted": 0, "rows_modified": 0,
                    "columns_changed": {}, "columns_added": {c: count(a, t, c) for c in added},
                    "columns_dropped": {c: count(b, t, c) for c in dropped}, "samples": {}}
            pk = mb[t]["pk"]
            if pk and pk == ma[t]["pk"]:
                rb = fetch_rows(b, t, pk, [c for c in common if c not in pk])
                ra = fetch_rows(a, t, pk, [c for c in common if c not in pk])
                cols = [c for c in common if c not in pk]
                info["rows_deleted"] = sum(1 for k in rb if k not in ra)
                info["rows_inserted"] = sum(1 for k in ra if k not in rb)
                changed_rows = 0
                for k in sorted(rb, key=lambda k: [int(x) if x and x.lstrip("-").isdigit() else 0 for x in k]):
                    if k not in ra or ra[k] == rb[k]:
                        continue
                    changed_rows += 1
                    for idx, col in enumerate(cols):
                        if rb[k][idx] != ra[k][idx]:
                            info["columns_changed"][col] = info["columns_changed"].get(col, 0) + 1
                            s = info["samples"].setdefault(col, [])
                            if len(s) < gatecore.SAMPLES_PER_COLUMN:
                                s.append({"pk": "/".join(k), "before": rb[k][idx], "after": ra[k][idx]})
                info["rows_modified"] = changed_rows
            else:
                info["no_primary_key"] = True
                info["rows_deleted"] = max(0, info["rows_before"] - info["rows_after"])
                info["rows_inserted"] = max(0, info["rows_after"] - info["rows_before"])
            if (info["rows_deleted"] or info["rows_inserted"] or info["rows_modified"] or added or dropped):
                info["status"] = "changed"
            if not info["samples"]:
                del info["samples"]
            out[t] = info
        # a table whose shape changed only through indexes/constraints is still "unchanged" data-wise
        return out
    finally:
        b.close(); a.close()


# ---------------------------------------------------------------- commands

def cmd_run(args):
    snap_path, mig_path = Path(args.snapshot), Path(args.migration)
    if not snap_path.exists():
        die(f"snapshot file not found: {snap_path}")
    if not mig_path.exists():
        die(f"migration file not found: {mig_path}")
    sql = mig_path.read_text()
    header, dump = load_snapshot(snap_path)
    srv = server()
    restore_ms = restore(srv, dump)

    with connect(srv, "before") as c:
        restored_fp = gatecore.fingerprint(c)
        engine = c.execute("SHOW server_version").fetchone()[0]
    if restored_fp != header["fingerprint"]:
        die("restored schema fingerprint differs from production's; the sandbox engine can't reproduce "
            "this schema faithfully (check Postgres major versions / extensions)")

    non_tx = gatecore.non_transactional_statements(sql)
    if non_tx:
        mig = {"ok": False, "error": "refused: statements that cannot run in a transaction",
               "sqlstate": None, "failed_statement": None, "statements": len(gatecore.split_statements(sql)),
               "duration_ms": 0}
        locks, stmt_log = {"max_write_lock_ms": 0, "tables": {}}, []
    else:
        mig, locks, stmt_log = run_migration(srv, sql)

    tables = diff(srv) if mig["ok"] else {}
    with connect(srv, "after") as c:
        after_fp = gatecore.fingerprint(c)

    findings = {"migration": mig, "non_transactional": non_tx, "tables": tables, "locks": locks,
                "statements": stmt_log, "schema_changed": after_fp != restored_fp}
    verdict, reasons = gatecore.compute_verdict(findings)
    report = {
        "report_version": gatecore.REPORT_VERSION,
        "snapshot_id": header["snapshot_id"],
        "snapshot_fingerprint": header["fingerprint"],
        "snapshot_dump_sha256": header["dump_sha256"],
        "target_database": header["target_database"],
        "migration_name": args.name or mig_path.name,
        "migration_sha256": gatecore.sql_sha256(sql),
        "engine": f"PostgreSQL {engine} (sandbox)",
        "rehearsed_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "findings": findings,
        "schema_after_fingerprint": after_fp,
        "verdict": verdict,
        "reasons": reasons,
        "summary": gatecore.summarize(findings, verdict),
    }
    report["digest"] = gatecore.report_digest(report)

    out = Path(args.out) if args.out else HOME / "report.json"
    out.write_text(gatecore.canonical_json(report))

    print(f"VERDICT: {verdict}")
    print(f"SUMMARY: {report['summary']}")
    for r in reasons:
        print(f"  - {r}")
    print(f"restore {restore_ms} ms; migration {mig['duration_ms']} ms; snapshot {header['snapshot_id']} "
          f"({header['target_database']}); engine {report['engine']}")
    for s in stmt_log:
        print(f"  stmt {s['n']}: {s['ms']} ms rows={s['rows']}{' ERROR' if s.get('error') else ''}  {s['sql']}")
    for t, info in tables.items():
        for col, samples in info.get("samples", {}).items():
            for s in samples:
                print(f"  sample {t}.{col} [{s['pk']}]: {s['before']!r} -> {s['after']!r}")
    print("databases kept for investigation: `before` (production copy) and `after` "
          f"({'migration applied' if mig['ok'] else 'rolled back, identical to before'})")
    print(f"REPORT_FILE: {out}")


def cmd_query(args):
    srv = server()
    import psycopg
    with psycopg.connect(srv.get_uri(args.db),
                         options="-c default_transaction_read_only=on -c statement_timeout=30s") as c:
        try:
            cur = c.execute(args.sql)
        except psycopg.Error as e:
            die(str(e).strip(), 1)
        if cur.description is None:
            print(f"OK ({cur.rowcount} rows)")
            return
        cols = [d.name for d in cur.description]
        rows = cur.fetchmany(args.limit + 1)
    more = len(rows) > args.limit
    rows = [["" if v is None else str(v) for v in r] for r in rows[:args.limit]]
    widths = [min(80, max([len(c)] + [len(r[i]) for r in rows])) for i, c in enumerate(cols)]
    print(" | ".join(c.ljust(w) for c, w in zip(cols, widths)))
    print("-+-".join("-" * w for w in widths))
    for r in rows:
        print(" | ".join(v[:80].ljust(w) for v, w in zip(r, widths)))
    print(f"({len(rows)} rows{', truncated' if more else ''})")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="restore snapshot, run migration, diff, write report")
    r.add_argument("--snapshot", required=True, help="file holding the export_snapshot result")
    r.add_argument("--migration", required=True, help="the migration .sql file, exactly as submitted")
    r.add_argument("--name", help="migration name for the report (default: file name)")
    r.add_argument("--out", help=f"report path (default {HOME}/report.json)")
    r.set_defaults(fn=cmd_run)
    q = sub.add_parser("query", help="read-only query against the kept rehearsal databases")
    q.add_argument("sql")
    q.add_argument("--db", choices=["before", "after"], default="before")
    q.add_argument("--limit", type=int, default=50)
    q.set_defaults(fn=cmd_query)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
