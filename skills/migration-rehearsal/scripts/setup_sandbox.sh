#!/usr/bin/env bash
# Installs what rehearse.py needs: psycopg + pgserver (a bundled Postgres 16). No sudo.
# Idempotent; prints READY when done. Takes ~20-40 s on a fresh sandbox.
set -uo pipefail
PY="${PYTHON:-python3}"
PKGS=("psycopg[binary]>=3.1" "pgserver>=0.1.4")

if "$PY" -c 'import psycopg, pgserver' 2>/dev/null; then
  echo "READY (already installed)"; exit 0
fi
"$PY" -c 'import sys; assert sys.version_info >= (3, 9), sys.version' || { echo "need Python 3.9+"; exit 1; }

pip_install() { "$PY" -m pip install --quiet --disable-pip-version-check --no-input "$@" "${PKGS[@]}"; }
pip_install 2>/dev/null \
  || pip_install --user 2>/dev/null \
  || pip_install --user --break-system-packages \
  || { echo "pip install failed"; exit 1; }

"$PY" - <<'PYEOF'
import psycopg, pgserver, pathlib
bin_dir = pathlib.Path(pgserver.__file__).parent / "pginstall" / "bin"
print(f"READY psycopg {psycopg.__version__}; bundled postgres at {bin_dir}")
PYEOF
