#!/usr/bin/env python3
"""One-off: deploy the two demo services (ambulance + police) by driving the
exact same provisioning logic the console's /api/deploy uses, but from the CLI.

Writes to the shared DynamoDB/S3, so the services appear in the live console at
console.appmesh.org, are routed by the live gateway and listed on appmesh.org.
"""
import sys
import uuid
from datetime import datetime
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import app

app._ensure_ddb_tables()
app._seed_root_user()
app._load_deployments_from_s3()

active = app._load_active_regions()
print("Active regions:", active, flush=True)
if not active:
    print("No onboarded regions; aborting.", flush=True)
    sys.exit(1)
region = active[0]

REPO_ROOT = Path(__file__).resolve().parent.parent

SERVICES = [
    ("ambulance-service", str(REPO_ROOT / "apps" / "ambulanta" / "target" / "ambulance-service.jar")),
    ("police-service",    str(REPO_ROOT / "apps" / "politia" / "target" / "police-service.jar")),
]


def already_live(name):
    sname = app._safe_service_name(name)
    for d in app.DEPLOYMENTS.values():
        if d.get("kind") in ("gateway", "console"):
            continue
        if d.get("status") in ("torn_down",):
            continue
        if app._safe_service_name(d.get("name", "")) == sname:
            return d
    return None


for name, jar_path in SERVICES:
    existing = already_live(name)
    if existing:
        print(f"SKIP {name}: already present "
              f"(id={existing['id']}, status={existing.get('status')})", flush=True)
        continue

    service_name = app._safe_service_name(name)
    dep_id = service_name + "-" + uuid.uuid4().hex[:8]
    now = datetime.utcnow().isoformat()
    steps_init = {s: {"status": "pending", "detail": "", "label": app.STEP_LABELS[s]}
                  for s in app.STEP_KEYS}
    dep = {
        "id": dep_id,
        "name": service_name,
        "service_name": service_name,
        "kind": "service",
        "owner": app.ROOT_USERNAME,
        "jar_filename": name + ".jar",
        "local_path": jar_path,
        "created_at": now,
        "status": "deploying",
        "region": region,
        "desired_instances": 1,
        "health_check_path": "/status",
        "version": 1,
        "version_deployed_at": now,
        "history": [],
        "steps": steps_init,
        "resources": {},
        "_version": 0,
    }
    app.DEPLOYMENTS[dep_id] = dep
    print(f"\n=== Deploying {service_name} ({dep_id}) to {region} ===", flush=True)
    try:
        app._run_deployment(dep_id, extra_regions=[])
        print(f"=== {service_name}: status={app.DEPLOYMENTS[dep_id].get('status')} "
              f"url=https://appmesh.org/{service_name}/ ===", flush=True)
    except Exception as e:
        print(f"!!! {service_name} FAILED: {e!r}", flush=True)
        import traceback
        traceback.print_exc()

print("\nAll done.", flush=True)
