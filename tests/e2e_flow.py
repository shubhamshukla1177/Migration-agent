#!/usr/bin/env python3
"""End-to-end guard test without TrueForge.

Plays the agent: calls the gate over MCP exactly like TrueForge does, runs rehearse.py
locally where the sandbox would, and checks every guard. Needs:
  - `make gate` running, a freshly seeded demo DB, an empty ledger (`make reset`)
  - GATE_URL (default http://127.0.0.1:8811/mcp), GATE_TOKEN (from .env)
  - ADMIN_URL: owner connection, used only to simulate drift / concurrent data changes
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "db_gate"))
sys.path.insert(0, str(ROOT / "skills" / "migration-rehearsal" / "scripts"))
import gatecore  # noqa: E402
import psycopg  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402
from server import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
logging.getLogger("httpx").setLevel(logging.WARNING)
GATE_URL = os.environ.get("GATE_URL", f"http://127.0.0.1:{os.environ.get('GATE_PORT', '8811')}/mcp")
TOKEN = os.environ.get("GATE_TOKEN", "")
ADMIN_URL = os.environ.get("ADMIN_URL", "")
LEDGER = Path(os.environ.get("GATE_STATE_DIR", ROOT / ".gate")) / "ledger.db"
REHEARSE = ROOT / "skills" / "migration-rehearsal" / "scripts" / "rehearse.py"
MIG = ROOT / "demo" / "migrations"
WORK = Path(tempfile.mkdtemp(prefix="gate-e2e-"))

passed, failed = [], []


def check(name: str, ok: bool, info=None):
    (passed if ok else failed).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok or info is None else f"\n        -> {info}"))


def admin(q: str):
    with psycopg.connect(ADMIN_URL, autocommit=True) as c:
        cur = c.execute(q)
        return cur.fetchall() if cur.description else None


class Gate:
    def __init__(self, session):
        self.s = session

    async def call(self, tool, **args):
        res = await self.s.call_tool(tool, args)
        if res.structuredContent is not None:
            return res.structuredContent
        text = res.content[0].text
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    async def snapshot(self) -> Path:
        text = await self.call("export_snapshot")
        path = WORK / f"snapshot_{len(list(WORK.glob('snapshot_*')))}.txt"
        path.write_text(text)
        return path


def rehearse(snapshot: Path, sql_path: Path) -> dict:
    out = WORK / (sql_path.stem + ".report.json")
    p = subprocess.run([sys.executable, str(REHEARSE), "run", "--snapshot", str(snapshot),
                        "--migration", str(sql_path), "--out", str(out)],
                       capture_output=True, text=True, env={**os.environ, "REHEARSAL_HOME": str(WORK / "pg")})
    if p.returncode != 0:
        raise SystemExit(f"rehearse.py failed:\n{p.stdout}\n{p.stderr}")
    return json.loads(out.read_text())


def write_sql(name: str, sql: str) -> Path:
    p = WORK / name
    p.write_text(sql)
    return p


def redigest(report: dict) -> str:
    report = dict(report)
    report["digest"] = gatecore.report_digest(report)
    return json.dumps(report)


async def run(gate: Gate):
    target = None

    print("\n1. the catch: 0007 fails on duplicate MBIs")
    snap1 = await gate.snapshot()
    header = json.loads(snap1.read_text().split("\n", 1)[0].split(" ", 2)[2])
    with psycopg.connect(os.environ["READER_URL"]) as c:
        check("export_snapshot returns a header matching prod's fingerprint",
              header["fingerprint"] == gatecore.fingerprint(c) and header["snapshot_id"].startswith("snap_"))
    r0007 = rehearse(snap1, MIG / "0007_enforce_unique_mbi.sql")
    s0007 = await gate.call("submit_rehearsal", report_json=json.dumps(r0007))
    check("0007 -> BLOCK (gate-recomputed)", s0007.get("accepted") and s0007["verdict"] == "BLOCK", s0007)
    a = await gate.call("apply_migration", rehearsal_id=s0007["rehearsal_id"],
                        migration_sql=(MIG / "0007_enforce_unique_mbi.sql").read_text(),
                        target_database=s0007["target_database"], rehearsal_summary=s0007["summary"])
    check("apply refuses a BLOCK verdict", a.get("refused") and "BLOCK" in a["reason"], a)

    print("\n2. reports can't be edited or relabelled")
    tampered = json.loads(json.dumps(r0007))
    tampered["findings"]["migration"]["ok"] = True
    t = await gate.call("submit_rehearsal", report_json=json.dumps(tampered))
    check("tampered report (digest mismatch) rejected", not t.get("accepted") and "digest" in t["reason"], t)
    relabel = json.loads(json.dumps(r0007))
    relabel["verdict"] = "SAFE"
    relabel["summary"] = relabel["summary"].replace("BLOCK", "SAFE", 1)
    t = await gate.call("submit_rehearsal", report_json=redigest(relabel))
    check("relabelled verdict rejected (gate recomputes from findings)",
          not t.get("accepted") and t.get("verdict") == "BLOCK", t)
    forged = json.loads(json.dumps(r0007))
    forged["snapshot_id"] = "snap_deadbeef"
    t = await gate.call("submit_rehearsal", report_json=redigest(forged))
    check("report for a snapshot the gate never issued rejected", not t.get("accepted"), t)

    print("\n3. the fix: v2 is SAFE, and apply guards hold")
    v2_path = MIG / "0007_enforce_unique_mbi_v2.sql"
    v2_sql = v2_path.read_text()
    r_v2 = rehearse(snap1, v2_path)
    s_v2 = await gate.call("submit_rehearsal", report_json=json.dumps(r_v2))
    check("v2 -> SAFE", s_v2.get("accepted") and s_v2["verdict"] == "SAFE", s_v2)
    target = s_v2["target_database"]
    good = dict(rehearsal_id=s_v2["rehearsal_id"], migration_sql=v2_sql, target_database=target,
                rehearsal_summary=s_v2["summary"])

    a = await gate.call("apply_migration", **{**good, "migration_sql": v2_sql + "\nDROP TABLE diagnoses;"})
    check("apply refuses SQL that wasn't rehearsed", a.get("refused") and "sha256" in a["reason"], a)
    a = await gate.call("apply_migration", **{**good, "target_database": "appdb@prod-replica"})
    check("apply refuses a different target_database", a.get("refused") and "target" in a["reason"], a)
    a = await gate.call("apply_migration", **{**good, "rehearsal_summary": "SAFE: adds a normalized MBI column"})
    check("apply refuses a paraphrased summary", a.get("refused") and "summary" in a["reason"], a)
    a = await gate.call("apply_migration", **{**good, "rehearsal_id": "rh_00000000"})
    check("apply refuses an unknown rehearsal", a.get("refused") and "unknown" in a["reason"], a)

    s_old = await gate.call("submit_rehearsal", report_json=json.dumps(r_v2))
    with sqlite3.connect(LEDGER) as db:
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25)).replace(microsecond=0).isoformat()
        db.execute("UPDATE rehearsals SET submitted_at = ? WHERE rehearsal_id = ?", (old, s_old["rehearsal_id"]))
    a = await gate.call("apply_migration", **{**good, "rehearsal_id": s_old["rehearsal_id"]})
    check("apply refuses a stale (>24h) rehearsal", a.get("refused") and "stale" in a["reason"], a)

    admin("ALTER TABLE patients ADD COLUMN drift_probe text")
    try:
        a = await gate.call("apply_migration", **good)
        check("apply refuses on schema drift since the snapshot", a.get("refused") and "drift" in a["reason"], a)
    finally:
        admin("ALTER TABLE patients DROP COLUMN drift_probe")

    a = await gate.call("apply_migration", **good)
    n = admin("SELECT count(mbi_normalized) FROM patients")
    check("v2 applies, and prod matches the rehearsal", a.get("applied") and n == [(5000,)], a)
    a = await gate.call("apply_migration", **good)
    check("apply refuses a double apply", a.get("refused") and "already applied" in a["reason"], a)

    print("\n4. the silent one: 0008 truncates ICD-10 codes without an error")
    p0008 = MIG / "0008_cap_icd10_length.sql"
    r_stale = rehearse(snap1, p0008)  # rehearsed on the snapshot taken before v2 landed
    s_stale = await gate.call("submit_rehearsal", report_json=json.dumps(r_stale))
    a = await gate.call("apply_migration", rehearsal_id=s_stale["rehearsal_id"], migration_sql=p0008.read_text(),
                        target_database=target, rehearsal_summary=s_stale["summary"], accept_review=True)
    check("apply refuses a rehearsal whose snapshot predates v2 (drift)",
          a.get("refused") and "drift" in a["reason"], a)
    snap2 = await gate.snapshot()
    r0008 = rehearse(snap2, p0008)
    s0008 = await gate.call("submit_rehearsal", report_json=json.dumps(r0008))
    changed = r0008["findings"]["tables"]["diagnoses"]["columns_changed"].get("icd10_code", 0)
    check("0008 -> REVIEW with ~4,100 rewritten codes",
          s0008.get("verdict") == "REVIEW" and 4000 <= changed <= 4300, s0008)
    a = await gate.call("apply_migration", rehearsal_id=s0008["rehearsal_id"], migration_sql=p0008.read_text(),
                        target_database=target, rehearsal_summary=s0008["summary"])
    check("apply refuses REVIEW without accept_review", a.get("refused") and "REVIEW" in a["reason"], a)

    print("\n5. data-dependent DDL: prod changes between rehearsal and apply -> rollback")
    dd = write_sql("0099_data_dependent.sql",
                   "DO $$ BEGIN\n  IF (SELECT count(*) FROM claims) > 15000 THEN\n"
                   "    EXECUTE 'CREATE INDEX claims_service_date_idx ON claims (service_date)';\n"
                   "  END IF;\nEND $$;")
    r_dd = rehearse(snap2, dd)
    s_dd = await gate.call("submit_rehearsal", report_json=json.dumps(r_dd))
    admin("INSERT INTO claims (patient_id, claim_no, rendering_npi, claim_type, service_date, paid_amount) "
          "VALUES (1, 'CLM-E2E-PROBE', '1000000004', 'professional', '2025-06-01', 1)")
    try:
        a = await gate.call("apply_migration", rehearsal_id=s_dd["rehearsal_id"], migration_sql=dd.read_text(),
                            target_database=target, rehearsal_summary=s_dd["summary"])
        idx = admin("SELECT 1 FROM pg_indexes WHERE indexname = 'claims_service_date_idx'")
        check("post-apply schema differs from rehearsal -> rolled back",
              a.get("refused") and a.get("status") == "rolled_back" and not idx, a)
    finally:
        admin("DELETE FROM claims WHERE claim_no = 'CLM-E2E-PROBE'")

    print("\n6. non-transactional statements")
    ci = write_sql("0098_concurrent_index.sql", "CREATE INDEX CONCURRENTLY claims_type_idx ON claims (claim_type);")
    r_ci = rehearse(snap2, ci)
    s_ci = await gate.call("submit_rehearsal", report_json=json.dumps(r_ci))
    a = await gate.call("apply_migration", rehearsal_id=s_ci["rehearsal_id"], migration_sql=ci.read_text(),
                        target_database=target, rehearsal_summary=s_ci["summary"])
    check("CREATE INDEX CONCURRENTLY -> BLOCK and refused",
          s_ci.get("verdict") == "BLOCK" and "non-transactional" in s_ci["summary"] and a.get("refused"), s_ci)

    print("\n7. migration_status (the Release Captain's entry ticket)")
    st_v2 = await gate.call("migration_status", migration_sql=v2_sql)
    st_0007 = await gate.call("migration_status", migration_sql=(MIG / "0007_enforce_unique_mbi.sql").read_text())
    st_0008 = await gate.call("migration_status", migration_sql=p0008.read_text())
    check("migration_status: v2 applied, 0007 blocked, 0008 ready_needs_review",
          (st_v2.get("state"), st_0007.get("state"), st_0008.get("state"))
          == ("applied", "blocked", "ready_needs_review"),
          (st_v2.get("state"), st_0007.get("state"), st_0008.get("state")))


async def main():
    if not TOKEN or not ADMIN_URL:
        raise SystemExit("set GATE_TOKEN (in .env) and ADMIN_URL")
    import httpx
    r = httpx.post(GATE_URL, json={})
    check("gate rejects requests without the bearer token", r.status_code == 401, r.status_code)
    async with streamablehttp_client(GATE_URL, headers={"Authorization": f"Bearer {TOKEN}"}) as (rd, wr, _):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            tools = {t.name: t for t in (await s.list_tools()).tools}
            check("apply_migration is annotated destructive",
                  tools["apply_migration"].annotations.destructiveHint is True
                  and tools["export_snapshot"].annotations.readOnlyHint is True)
            await run(Gate(s))
    print(f"\n{len(passed)} passed, {len(failed)} failed  (work dir {WORK})")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
