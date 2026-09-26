# Loads .env for targets that need it
include $(wildcard .env)
export

PY ?= python3
GATE_PORT ?= 8811

install:            ## install Python deps
	$(PY) -m pip install -r requirements.txt

seed:               ## seed YOUR demo prod DB: make seed ADMIN_URL=... READER_PW=... MIGRATOR_PW=...
	$(PY) demo/seed.py --admin-url "$(ADMIN_URL)" --reader-password "$(READER_PW)" --migrator-password "$(MIGRATOR_PW)"

gate:               ## run the db-gate MCP server (TrueForge connects to http://localhost:8811/mcp)
	$(PY) db_gate/server.py

agent:              ## register/update the agent in TrueForge local
	$(PY) agent/register.py agent/migration-rehearsal.agent.json

test:               ## end-to-end check without TrueForge (needs `make gate` running + freshly seeded DB)
	GATE_URL=http://127.0.0.1:$(GATE_PORT)/mcp $(PY) tests/e2e_flow.py

reset:              ## forget snapshots/rehearsals/applications (after re-seeding the demo DB)
	rm -rf .gate

.PHONY: install seed gate agent test reset
