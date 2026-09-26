# release-gate: migration rehearsal on TrueForge

An agent that takes a Postgres migration, **restores a real copy of production into a
TrueForge sandbox, runs the migration there, diffs what happened to every row**, and reports
back. It applies the migration to production only through a tool that **pauses for a human
and refuses anything that wasn't rehearsed**.

The demo runs on a **synthetic Medicare ACO claims database** of attributed beneficiaries
keyed by MBI, their claims, and ICD-10-CM diagnoses. It's the kind of data where a
"harmless" migration can quietly double-count members or wipe out billable diagnosis codes.
No real PHI: every name, MBI, NPI and claim is generated.

This repo is the core of a two-agent build. The Release Captain (commits → tests →
release notes → tag/publish) comes next and uses `migration_status` from the same gate
as its entry ticket.

```
TrueForge local (npx)                          your machine
┌──────────────────────────────┐               ┌──────────────────────────────────┐
│ migration-rehearsal agent    │  MCP (HTTP,   │ db-gate  (db_gate/server.py)     │
│  • skill: migration-rehearsal│  bearer auth) │  export_snapshot   read-only role│──► prod Postgres
│  • tool approval: apply_*    │◄─────────────►│  submit_rehearsal  ledger only   │    (Neon/Supabase)
└──────────────┬───────────────┘               │  apply_migration ⛔ write role    │
               │ sandbox tool                  └──────────────────────────────────┘
┌──────────────▼───────────────┐
│ Daytona sandbox (no creds)   │
│  rehearse.py: restore → run  │
│  → row diff → verdict        │
└──────────────────────────────┘
```

## How the pieces meet the three requirements

| Requirement | Where it happens |
|---|---|
| **Reaches something real** | `db-gate` MCP server → your Postgres, with a read-only role and a separate migration role |
| **Runs what it writes** | `rehearse.py` in the TrueForge sandbox: a real Postgres 16, a real restore, a real migration run, and the agent's own investigation queries |
| **Knows when to stop** | `apply_migration` is annotated destructive and listed in `require_approval_for_tools`, so TrueForge pauses with Allow/Deny. The gate also enforces the rules itself (below). |

The snapshot reaches the sandbox without the sandbox ever holding credentials.
`export_snapshot` returns the dump as one large text result. TrueForge's large-tool-response
handling writes any MCP result over about 6k tokens into the sandbox (`/opt/tf/tool-results/…`)
and gives the agent only the path, and `rehearse.py run --snapshot <path>` reads it from there.

### What `apply_migration` refuses (in code, not in the prompt)

- SQL whose sha256 differs from the rehearsed file
- a **BLOCK** verdict. For **REVIEW**, it requires `accept_review=true`.
- a `target_database` or `rehearsal_summary` that doesn't match the gate's record. These are
  arguments on purpose: the approval card shows the real target and the real impact line.
- **schema drift**: prod's fingerprint changed since the snapshot
- a post-apply schema that differs from the rehearsal result. It **rolls back** instead.
- a stale rehearsal (over 24 h), a migration that's already applied, or non-transactional statements

The gate also recomputes the verdict from raw findings on `submit_rehearsal` rather than
trusting the report's label, and a digest over the report catches edits in transit.

Verdict rules (`gatecore.compute_verdict`, shared by sandbox and gate):

| Verdict | When |
|---|---|
| **BLOCK** | the migration errors; non-transactional statements; rows deleted; a table or a non-empty column dropped |
| **REVIEW** | existing values rewritten; rows inserted into existing tables; a write-blocking lock held > 2 s |
| **SAFE** | everything else, e.g. new columns/indexes with no existing value changed |

## Setup (about 15 minutes)

**Prerequisites:** Python 3.10+, Node 22.14+, `pg_dump` at or above your server's major
version, a Postgres you own (Neon or Supabase free tier works), a model API key, and a
Daytona API key for the sandbox. The skill is fetched from Git, so this repo has to be
pushed to GitHub or GitLab.

1. **Install**
   ```bash
   git clone <this repo> && cd release-gate
   python3 -m venv .venv && source .venv/bin/activate && make install
   ```
   No Python 3.10+ on the machine? `uv venv --python 3.12 .venv` gets you one.

2. **Seed your own "prod"**, with demo tables plus the two least-privilege roles:
   ```bash
   make seed ADMIN_URL='postgresql://owner:...@host/appdb?sslmode=require' READER_PW='...' MIGRATOR_PW='...'
   # → patients=5000 claims=15000 diagnoses=39727
   #     duplicate beneficiaries across feeds: 37
   #     dx codes longer than 6 chars (C50.911, E11.319, E11.649, J45.909, S72.001A): 4159
   ```
   The seed creates `gate_reader` (SELECT only, read-only transactions), `gate_migrator`
   (member of `app_owner`, which owns the tables) and the tables.
   There's no cloud database? `docker compose up -d` gives you one on port 5433. Any local
   Postgres 16 works too.

3. **Configure and start the gate**
   ```bash
   cp .env.example .env     # fill in the two role URLs + a token: openssl rand -hex 24
   make gate                # → MCP at http://127.0.0.1:8811/mcp
   ```

4. **Start TrueForge local** in a second terminal. It blocks private and loopback URLs by default, so
   allow `localhost` for the gate:
   ```bash
   OUTBOUND_URL_ALLOWED_HOSTS='["localhost"]' npx @truefoundry/trueforge@latest   # UI at http://localhost:8790
   ```
   - **Settings → Models:** add your provider key.
   - **Settings → Sandbox:** add your Daytona API key.

5. **Create the agent, connector and skill**
   ```bash
   make agent               # or: python agent/register.py agent/migration-rehearsal.agent.json --model openai/gpt-5-5
   ```
   `register.py` uses the TrueForge API to upsert three things:
   - the `db-gate` MCP server: `http://localhost:8811/mcp` with header `Authorization: Bearer <GATE_TOKEN>`
   - the `migration-rehearsal` skill: `SKILL_REPO` at `SKILL_REF`, subdirectory `skills/migration-rehearsal`
   - the agent itself, with the sandbox on and approval required for `apply_migration`

   You can also do all of this in the UI (**Settings → Connectors / Skills**, then build the
   agent). If TrueForge runs in Docker instead of npx, use `http://host.docker.internal:8811/mcp`
   (`GATE_PUBLIC_URL` in `.env`), set `GATE_HOST=0.0.0.0`, and allow that host instead.

## Demo script (what to film)

1. **The catch.** Prompt: *"Rehearse `demo/migrations/0007_enforce_unique_mbi.sql`"* (paste it).
   The migration normalises MBIs to CMS format and adds a unique index, "so attribution never
   double-counts a member". The agent snapshots prod, restores it in the sandbox, and runs the
   migration, which fails on a duplicate key. It then queries the kept sandbox DB and finds
   **37 beneficiaries who arrived from both the CCLF feed (`1EG4TE5MK72`) and the EHR feed
   (`1eg4-te5-mk72`)**, each with their own claims. The gate returns **BLOCK**, and the agent
   proposes a fix: a member-merge first, then uniqueness.
2. **The fix.** Rehearse `0007_enforce_unique_mbi_v2.sql`. Result: **SAFE**. No source rows
   are modified, `mbi_normalized` is populated on all 5,000 patients and indexed for joins,
   and the write-lock time is shown.
3. **The stop.** *"Apply it."* TrueForge pauses on `apply_migration`: the card shows the
   exact SQL, the target, and the gate-verified impact line. Film this, then approve.
4. **The silent one.** `0008_cap_icd10_length.sql` caps `icd10_code` at 6 characters.
   ICD-10-CM codes are 7 characters at most *without* the dot, but these are stored *with* it.
   The migration runs **without any error**, yet rewrites 4,159 diagnoses:
   `E11.649 → E11.64`, `S72.001A → S72.00`, `C50.911 → C50.91`. Every result is a category
   header, not a billable code, so the diabetes, hip-fracture and breast-cancer diagnoses
   silently become unbillable. The rehearsal marks it **REVIEW** and shows the before/after
   values. This is the strongest argument for rehearsing: no error message would ever have
   flagged it.

On camera, show the sandbox tool calls (restore, migration run, investigation queries).
That's the "where the code ran" proof.

## Verify without TrueForge

`tests/e2e_flow.py` plays the agent's role. It calls the gate over MCP exactly as
TrueForge does, runs `rehearse.py` locally, and runs 23 checks: bearer auth,
BLOCK/SAFE/REVIEW, tampered and relabelled reports, wrong SQL, wrong target, paraphrased
summary, unknown and stale rehearsals, drift (manual and after a real apply), double apply,
rollback on a post-apply schema mismatch, non-transactional statements, and `migration_status`.

```bash
make reset && make seed ...     # fresh demo DB + empty ledger
make gate &                     # in another terminal
ADMIN_URL='postgresql://owner:...' make test
```
`ADMIN_URL` is used only to simulate drift and a concurrent data change. The run applies
v2 to the demo DB, so reseed before running it again.

## Layout

```
db_gate/server.py                         MCP server: the only holder of DB credentials
skills/migration-rehearsal/SKILL.md       the procedure the agent follows
skills/migration-rehearsal/scripts/
  rehearse.py                             runs in the sandbox: restore, run, diff, verdict
  gatecore.py                             fingerprint + verdict rules shared by sandbox and gate
  setup_sandbox.sh                        installs psycopg + bundled Postgres 16 (no sudo)
agent/                                    agent spec + registration via TrueForge API
demo/                                     seed script + the three demo migrations
tests/e2e_flow.py                         end-to-end guard test
```

## Honest limits

- **Snapshots contain real rows.** The dump passes through the harness into a Daytona
  sandbox. **Never point this at real PHI.** Use the synthetic demo data or a de-identified
  copy. Column masking in `export_snapshot` (for MBI, names and birth dates) would be the step
  before any real healthcare use, along with a BAA covering every hop.
- **The digest catches accidents, not a model trying to deceive.** A deliberately forged
  report could still pass `submit_rehearsal`. The human approval and the verdict the gate
  recomputes are the backstop. Signing reports inside the sandbox would close this gap.
- **The sandbox runs Postgres 16** (the `pgserver` wheel). If prod's schema doesn't restore to
  the same fingerprint there, for example because of extensions or a newer major version,
  `rehearse.py` refuses rather than rehearsing against something that isn't prod.
- **Row diffs load each table into memory** in the sandbox. That's fine for demo-sized data;
  bigger tables would need hashed chunks.
- **Lock timings reflect sandbox hardware**, so treat them as relative, not absolute.
- **Migrations run as one transaction.** `CREATE INDEX CONCURRENTLY` and similar statements are refused.
- **The ledger (`.gate/ledger.db`) is local.** Run `make reset` after re-seeding the demo DB.

## Built with

- [TrueForge](https://github.com/truefoundry/trueforge) (agent harness, sandbox, tool approval)
- MCP Python SDK, psycopg 3, and the `pgserver` wheel (bundled Postgres)
- **AI assistants used while building:** Claude (Anthropic) for design and code.
