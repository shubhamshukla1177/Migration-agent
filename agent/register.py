#!/usr/bin/env python3
"""Register (or update) the migration-rehearsal agent in TrueForge through its HTTP API.

  python agent/register.py agent/migration-rehearsal.agent.json [--model openai/gpt-5-5]
                           [--no-connectors] [--trueforge http://localhost:8790]

Unless --no-connectors is given, it also upserts the two things the agent references:
  - MCP server `db-gate`   -> GATE_PUBLIC_URL (default http://localhost:<GATE_PORT>/mcp), Bearer GATE_TOKEN
  - skill `migration-rehearsal` -> SKILL_REPO @ SKILL_REF, path skills/migration-rehearsal
Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path):
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def api(base: str, method: str, path: str, body=None):
    req = urllib.request.Request(base.rstrip("/") + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json", "accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {path} -> {e.code}: {e.read().decode(errors='replace')[:1500]}")
    except urllib.error.URLError as e:
        sys.exit(f"can't reach TrueForge at {base}: {e.reason}. Is `npx @truefoundry/trueforge` running?")


def upsert_connectors(base: str, skill_name: str, mcp_name: str):
    token = os.environ.get("GATE_TOKEN")
    if not token:
        sys.exit("GATE_TOKEN is not set (.env); needed to register the db-gate MCP server")
    url = os.environ.get("GATE_PUBLIC_URL") or f"http://localhost:{os.environ.get('GATE_PORT', '8811')}/mcp"
    api(base, "PUT", "/api/v1/settings/mcp-servers", {"manifest": {
        "type": "remote", "name": mcp_name, "url": url,
        "description": "Production Postgres gatekeeper: read-only snapshots, rehearsal ledger, approval-gated apply.",
        "auth": {"type": "header", "headers": {"Authorization": f"Bearer {token}"}}}})
    print(f"MCP server  {mcp_name} -> {url}")

    repo, ref = os.environ.get("SKILL_REPO"), os.environ.get("SKILL_REF", "main")
    if not repo or "<you>" in repo:
        print("skill       skipped: set SKILL_REPO (a GitHub/GitLab URL of this repo) and SKILL_REF in .env")
        return
    api(base, "PUT", "/api/v1/settings/skills", {"manifest": {
        "type": "git", "name": skill_name, "url": repo, "path": "skills/migration-rehearsal", "ref": ref,
        "description": "Rehearse a Postgres migration on a production snapshot in the sandbox, diff every row, "
                       "get a gate-verified verdict, and apply only through the approval-gated gate."}})
    print(f"skill       {skill_name} -> {repo} @ {ref} (skills/migration-rehearsal)")


def main():
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("--model", help="model FQN as listed by TrueForge, e.g. openai/gpt-5-5")
    ap.add_argument("--trueforge", default=os.environ.get("TRUEFORGE_URL", "http://localhost:8790"))
    ap.add_argument("--no-connectors", action="store_true", help="don't upsert the MCP server / skill")
    args = ap.parse_args()

    spec = json.loads(Path(args.spec).read_text())
    if args.model:
        spec["manifest"]["model"]["name"] = args.model
    base = args.trueforge

    models = [m["name"] for m in api(base, "GET", "/api/v1/models").get("data", [])]
    if spec["manifest"]["model"]["name"] not in models:
        sys.exit(f"model {spec['manifest']['model']['name']!r} isn't configured in TrueForge. "
                 f"Available: {', '.join(models) or '(none: add a provider in Settings -> Models)'}. Use --model.")

    if not args.no_connectors:
        upsert_connectors(base, spec["manifest"]["skills"][0]["name"], spec["manifest"]["mcp_servers"][0]["name"])

    existing = api(base, "GET", "/api/v1/agents?" + urllib.parse.urlencode({"name": spec["name"], "limit": 100}))
    match = next((a for a in existing.get("data", []) if a.get("name") == spec["name"]), None)
    if match:
        res = api(base, "PUT", f"/api/v1/agents/{match['id']}", spec)
        verb = "updated"
    else:
        res = api(base, "POST", "/api/v1/agents", spec)
        verb = "created"
    agent = res.get("data", res)
    print(f"agent       {spec['name']} {verb} (id {agent.get('id')}, model {spec['manifest']['model']['name']})")
    print(f"open        {base.rstrip('/')}  -> pick '{spec['name']}' and paste a migration")


if __name__ == "__main__":
    main()
