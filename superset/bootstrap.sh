#!/bin/bash
set -e

echo "Running Superset DB migration..."
superset db upgrade

echo "Creating admin user (idempotent)..."
superset fab create-admin \
  --username "${ADMIN_USERNAME}" \
  --firstname "${ADMIN_FIRST_NAME}" \
  --lastname "${ADMIN_LAST_NAME}" \
  --email "${ADMIN_EMAIL}" \
  --password "${ADMIN_PASSWORD}" 2>&1 | grep -v "already exists" || true

echo "Initializing Superset..."
superset init

# Import the declarative asset bundle (databases, datasets, charts, dashboards).
# Every *.yaml under /app/assets is read, run through os.path.expandvars so
# placeholders like ${ANALYTICS_DB_URI} resolve from the container env, and
# handed to ImportAssetsCommand — the same code path as /api/v1/assets/import/.
# Upserts by UUID, so re-running keeps assets in sync on every start.
echo "Importing assets bundle..."
python3 - <<'PY'
import os
import pathlib
import sys
from superset.app import create_app

app = create_app()
with app.test_request_context():
    from flask import g
    from flask_login import login_user
    from superset.commands.importers.v1.assets import ImportAssetsCommand
    from superset.extensions import security_manager

    admin = security_manager.find_user(os.environ["ADMIN_USERNAME"])
    if admin is None:
        print("Admin user not found; bootstrap order is wrong.", file=sys.stderr)
        sys.exit(1)
    login_user(admin)
    g.user = admin

    src = pathlib.Path("/app/assets")
    contents = {
        p.relative_to(src).as_posix(): os.path.expandvars(p.read_text())
        for p in src.rglob("*.yaml")
    }
    try:
        ImportAssetsCommand(contents=contents).run()
    except Exception as exc:
        import re
        import traceback
        unresolved = sorted({
            m.group(1)
            for body in contents.values()
            for m in re.finditer(r"\$\{([A-Z_][A-Z0-9_]*)\}", body)
        })
        if unresolved:
            print(
                f"Hint: the bundle still contains unsubstituted placeholders: "
                f"{unresolved}. Set these in .env (or the superset service env) "
                f"and recreate the container.",
                file=sys.stderr,
            )
        print("Asset import failed. Full chain:", file=sys.stderr)
        traceback.print_exception(type(exc), exc, exc.__traceback__)
        cause = exc.__cause__ or exc.__context__
        while cause is not None:
            print("--- Caused by ---", file=sys.stderr)
            traceback.print_exception(type(cause), cause, cause.__traceback__)
            cause = cause.__cause__ or cause.__context__
        if hasattr(exc, "exceptions"):
            for sub in exc.exceptions:
                print("--- Sub-exception ---", file=sys.stderr)
                traceback.print_exception(type(sub), sub, sub.__traceback__)
        sys.exit(1)
    print(f"Imported {len(contents)} asset file(s): {sorted(contents)}")
PY

echo "Starting Superset..."
exec /usr/bin/run-server.sh
