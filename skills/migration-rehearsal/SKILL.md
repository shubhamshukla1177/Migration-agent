---
name: migration-rehearsal
description: Rehearse a Postgres migration on a real copy of production inside the sandbox, diff every row, get a gate-verified SAFE / REVIEW / BLOCK verdict, and apply it only through the approval-gated db-gate tool. Use whenever someone asks to check, test, rehearse, review or apply a SQL migration.
---

# Migration rehearsal

You prove what a migration does to production data **before** it touches production.
Production credentials never enter the sandbox: the `db-gate` MCP server holds them and
hands you a snapshot, verifies your report, and runs the final apply behind a human approval.

`SKILL_DIR` is this skill's directory (normally `/opt/tf/skills/migration-rehearsal`).
All commands below run in the sandbox.

## 1. Prepare the sandbox (once per session)

```bash
bash $SKILL_DIR/scripts/setup_sandbox.sh
```
Wait for `READY`. If the command times out, run it in the background
(`nohup bash $SKILL_DIR/scripts/setup_sandbox.sh > /tmp/setup.log 2>&1 &`) and poll
`tail -1 /tmp/setup.log` until it says `READY`.

## 2. Save the migration exactly as given

Write the user's SQL byte-for-byte with a quoted heredoc. Don't reformat it or "fix" it.
Keep the original file name.
```bash
mkdir -p /tmp/migrations && cat > /tmp/migrations/0007_enforce_unique_mbi.sql <<'SQL'
...the SQL, unchanged...
SQL
```

## 3. Snapshot production

Call **`db-gate` → `export_snapshot`** (no arguments). The result is several hundred KB, so the
harness saves it into the sandbox and replies with `Result saved to: <path>`. Use that path.
Never `cat` the file or paste its contents.

## 4. Rehearse

```bash
python3 $SKILL_DIR/scripts/rehearse.py run --snapshot <path from step 3> \
        --migration /tmp/migrations/<name>.sql --out /tmp/migrations/<name>.report.json
```
This restores the snapshot into a local Postgres (`before`), clones it (`after`), and runs the
migration on `after` in one transaction. It records every statement's time and the write locks
it held, then diffs every row of every table. It prints `VERDICT`, `SUMMARY`, sample
before→after values, and the report path.

## 5. Investigate (always for BLOCK or REVIEW)

Both databases stay up. Query them read-only to explain the cause with real numbers and
examples:
```bash
python3 $SKILL_DIR/scripts/rehearse.py query --db before "SELECT ..."
python3 $SKILL_DIR/scripts/rehearse.py query --db after  "SELECT ..."
```
- A failed migration rolls back, so `after` is identical to `before`. Query `before` to see which
  rows break it (for example duplicates behind a unique-index failure: group by the normalised
  key, count, and show the rows and their child records).
- For silent rewrites, compare values in `before` and `after`, count them by value, and say what
  the new values mean in the domain. For example, an ICD-10-CM code cut down to a category header
  is no longer billable.

## 6. Submit the report to the gate

```bash
cat /tmp/migrations/<name>.report.json
```
Call **`db-gate` → `submit_rehearsal`** with `report_json` set to that file's contents, **verbatim**.
It's one line of JSON, and any edit fails the digest check. The gate recomputes the verdict and
returns `rehearsal_id`, `verdict`, `summary` and `target_database`.

## 7. Report back

Lead with the gate's verdict and summary, then give the evidence (counts, example rows, lock
times, what the statements did). For **BLOCK** or **REVIEW**, propose a fix as a new migration
file with a clear name (for example `..._v2.sql`), and say you'll rehearse it if they want. Don't
apply anything in this step.

## 8. Apply (only when the user explicitly asks)

Call **`db-gate` → `apply_migration`** with:
- `rehearsal_id` from `submit_rehearsal`
- `migration_sql`: exactly the SQL that was rehearsed
- `target_database`: exactly as `submit_rehearsal` returned it
- `rehearsal_summary`: the gate's `summary`, **verbatim**
- `accept_review`: `true` only if the verdict is REVIEW **and** the user explicitly said they
  accept those findings. Otherwise leave it out.

The call pauses for a human Allow/Deny. If the gate refuses (BLOCK, stale, drift, SQL mismatch,
already applied, rolled back), report the reason as is. Don't work around it: re-snapshot and
re-rehearse when the reason calls for that. Use `migration_status` to check where a migration stands.

## Rules

- Never try to reach production from the sandbox. The sandbox has no credentials, and that's deliberate.
- Never edit a report, a verdict or a summary. The gate recomputes all three.
- Only rehearse migrations that were given to you or that you proposed and labelled as yours.
- A rehearsal runs on a snapshot. Tell the user if production may have changed since.
