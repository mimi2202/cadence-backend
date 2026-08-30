#!/usr/bin/env bash
# Run once from the repo root (the folder containing backend/ and frontend/).
#
# Fixes what the flat restructure broke:
#   - relative imports (from .db) only work inside a package; you have no app/
#   - App.jsx imports ./api.js but the file moved to api/api.js
set -euo pipefail

echo "→ converting relative imports to absolute"
cd backend
for f in bounces.py config.py db.py ingest.py main.py personalize.py transports.py unsubscribe.py worker.py test_bounces.py; do
  [ -f "$f" ] || continue
  # from .db import x   ->  from db import x
  sed -i.bak -E 's/^from \.([a-z_]+) import/from \1 import/' "$f"
  # from . import unsubscribe  ->  import unsubscribe
  sed -i.bak -E 's/^from \. import (.+)$/import \1/' "$f"
  rm -f "$f.bak"
done
cd ..

echo "→ fixing the api import in App.jsx"
sed -i.bak 's#from "./api.js"#from "./api/api.js"#' frontend/App.jsx && rm -f frontend/App.jsx.bak

echo "→ done. Now: pip install -r backend/requirements.txt"