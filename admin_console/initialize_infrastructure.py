#!/usr/bin/env python3
"""initialize_infrastructure — thin CLI around the provisioning logic in app.py.

The actual provisioning lives in app.py (so the console runs it natively too); this
is just a command-line entry point. The ACTIVE (onboarded) region set is DERIVED
from the DynamoDB deployment table — a region is active iff it has a deployed
gateway. The menu of selectable regions is app.SUPPORTED_REGIONS.

Usage:
  python initialize_infrastructure.py init [region ...]    # whole infra: console + landing + gateway(s)
  python initialize_infrastructure.py redeploy-console      # roll existing console ASG to current code package
  python initialize_infrastructure.py redeploy-gateway [region]  # roll gateway ASG(s) to the freshly built gateway JAR
  python initialize_infrastructure.py deploy-landing       # deploy just the appmesh.org landing service
  python initialize_infrastructure.py add-region <region>  # onboard one region
  python initialize_infrastructure.py remove-region <region>
  python initialize_infrastructure.py teardown             # tear everything down
  python initialize_infrastructure.py status               # show supported + active regions
"""
import sys

# Force UTF-8 stdout/stderr so log lines containing '…', '→', '✓' etc. don't crash
# on Windows' default cp1252 console/redirected output.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import app

USAGE = __doc__


def main():
    args = sys.argv[1:]
    if not args:
        print(USAGE)
        return
    cmd, rest = args[0], args[1:]

    # Active regions are derived from the deployment table, so populate it first.
    if cmd == "status":
        app._load_deployments_from_s3()
        print("Console region:", app.CONSOLE_REGION)
        print("Max onboarded:", app.MAX_ONBOARDED_REGIONS)
        print("Supported     :", ", ".join(app.SUPPORTED_REGIONS))
        print("Active (DDB)  :", ", ".join(app._load_active_regions()) or "(none)")
        return

    # Mutating commands: make sure the control-plane tables exist and the
    # in-memory deployment registry is populated (so teardown can find stacks).
    app._ensure_ddb_tables()
    app._seed_root_user()
    app._load_deployments_from_s3()

    if cmd == "init":
        app.init_all(regions=rest or None, log=print)
    elif cmd == "redeploy-console":
        app.redeploy_console(log=print)
    elif cmd == "redeploy-gateway":
        app.redeploy_gateway(rest[0] if rest else None, log=print)
    elif cmd == "deploy-landing":
        app.deploy_landing(log=print)
    elif cmd == "add-region" and rest:
        app.add_region(rest[0], log=print)
    elif cmd == "remove-region" and rest:
        app.remove_region(rest[0], log=print)
    elif cmd == "teardown":
        app.teardown_all(log=print)
    else:
        print(USAGE)


if __name__ == "__main__":
    main()
