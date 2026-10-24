# AppMesh QA Findings

Record of the four-phase QA pass over the platform, kept as supporting material for
the dissertation. The harness that produced it (`_qa_lib.py`, `_qa_phase1..4.py`) was
a throwaway local script set and is not part of this repository, so the file names it
mentions will not be found here.

## Phase 1 — Console functional + edge cases

### FIXED: F-001 — User service deployments missing `kind` field in DDB

**Repro:** Deploy any service via `/api/deploy`. Check DDB record → `kind` field absent (`None`).

**Expected:** DDB record has `kind = "service"` so it's consistent with infrastructure records
(`gateway`, `console`, `landing`) set via `_register_infra_deployment`.

**Impact:** Scan health step infers `kind` from name (works around it), but the `/deployment/<id>`
detail page and any code path relying on `dep.get("kind")` returns `None` for user services,
making `_is_user_service()` work only via negation. The DDB record is the source of truth and
should be explicit.

**Fix:** Added `"kind": "service"` to the `dep` dict in `api_deploy` (app.py:3976).

**Verified:** Phase 2 DDB check confirms `kind=service` in DDB after deploy.

---

### FIXED: F-002 — Service name sanitization is no longer silent (`"INVALID NAME!" → rejected`)

**Behavior:** `_safe_service_name()` sanitizes the input before validation.
`"INVALID NAME!"` becomes `"invalid-name"` (valid slug) and the deployment proceeds.

**Fix:** `/api/deploy` now requires the submitted service name to already be canonical. Names must
be lowercase DNS-safe slugs (`a-z`, `0-9`, hyphens, no leading/trailing hyphen). Non-canonical
input returns HTTP 400 instead of silently changing the hostname.

---

### FIXED: F-003 — Account deletion does not leave owned deployments orphaned

**Behavior:** Deleting a user account leaves their deployments in DDB with the deleted username
as `owner`. Those deployments become orphaned — visible only to root.

**Impact:** Root can still see and teardown orphaned deployments. No data loss, but it can be
confusing. Low priority for MVP.

**Fix:** Account deletion now refuses with HTTP 409 while the user owns any active user-service
deployments. Operators must teardown or transfer those deployments first, which avoids silent
orphan rows and avoids destructive surprise cascade teardown.

---

## Phase 2 — Single-region lifecycle

### ALL PASS — No issues found

Verified end-to-end:
- Deploy: ALB rule, TG healthy, ASG InService, SG+LT+IAM present, CF CNAME, S3 ETag changes, DDB status+kind.
- Scale 1→3→1: ASG and TG health follow correctly.
- Update JAR: version increments to 2, DDB updated, TG stays healthy.
- Teardown: ASG/TG/SG/LT/IAM/CF CNAME/DDB all gone; shared ALB preserved; S3 config ETag changes; `qa-svc` absent from gateway config.

---

## Phase 3 — Persistence / resilience

### ALL PASS — No issues found

- **Teardown survives session change:** Worker thread continues after logout. Dep visible as
  `tearing_down` after login. All AWS resources cleaned.
- **Interrupted teardown resumed:** `_resume_interrupted()` correctly picks up the teardown from
  `pipelines/{dep_id}-teardown.json`, skips completed steps, finishes the rest. AWS clean.
- **Interrupted deploy rolled back:** `_resume_interrupted()` for `status=deploying` runs
  `_teardown_resources_simple`, marks failed, removes from DDB. No orphan resources.

---

## Phase 4 — Multi-region + induced failover

### FIXED: F-004 — `get_alb()` / `get_listener()` / `get_listener_rules()` crash on missing ALB

**Repro:** Call `get_alb(region)` when the ALB does not exist. `_aws()` returns a `CompletedProcess`
(non-zero exit), then `(r or {}).get("LoadBalancers")` fails: `AttributeError: 'CompletedProcess'
object has no attribute 'get'`. Same bug in `get_listener()` and `get_listener_rules()`.

**Impact:** Phase 4-E (remove-region verification) crashed after CF pool checks passed, before the
`alb_ec1 is None` assertion. The regional ALB WAS actually deleted (confirmed separately); only the
test verification crashed.

**Fix:** Changed all three helpers in `_qa_lib.py` to use `_aws_dict(r)` (already existed for
`sg_exists`, `tg_exists`, etc.) instead of `(r or {})`.

**Verified:** `get_alb("eu-central-1")` returns `None` after the regional ALB is removed.

---

### FIXED: F-005 — Phase 4-A CF pool check is a false FAIL (timing: pool created after op "done")

**Repro:** After `add-region eu-central-1` operation completes (gateway-ec1 deployed, op status
changes to non-running), checking `cf_pool_present("ec1")` immediately returns False. However,
Phase 4-E `cf_pool_present("ec1")` returned True (pool was deleted successfully), proving it WAS
created — just after the initial check.

**Impact:** False FAIL in Phase 4-A. The pool is eventually created; it's a timing gap between the
op status changing to done and the CF pool creation step completing.

**Fix:** Added a 90-second retry loop in `test_add_region()` for the CF pool check.

**Verified:** Phase 4 re-run passed region onboarding and created the `appmesh-ec1` pool.

---

### FIXED: F-006 — Teardown `td_iam_role` / `td_instance_profile` mark `failed` for NoSuchEntity

**Repro:** If the IAM role or instance profile was already deleted externally (or by a prior
partial teardown), `iam.delete_role()` / `iam.delete_instance_profile()` throw
`NoSuchEntityException` (a `ClientError`). The outer `except Exception` catches it and marks the
step `failed`, leaving the deployment in `teardown_partial` instead of completing cleanly.

**Observed:** `qa-svc-p3b-a739a48e` stuck in `teardown_partial` with `td_iam_role: failed /
NoSuchEntity`. All other teardown steps were done; actual AWS resources confirmed clean.

**Impact:** Operator sees a broken deployment in the console list that cannot be retried. DDB
record persists indefinitely.

**Fix:** In `app.py`, both `td_instance_profile` and `td_iam_role` now catch `ClientError` with
code `NoSuchEntityException` and treat it as success (resource already gone = desired state).

**Verified:** Retrying `qa-svc-p3b-a739a48e` teardown after its IAM role/profile were already gone
completed and removed the partial row.

---

### FIXED: F-007 — Stale CF LB pool when gateway torn down via teardown API

**Repro:** Tear down a gateway deployment via the standard teardown API (not via `remove_region`).
The `_run_teardown` pipeline deletes ASG/TG/SG/LT/IAM/DNS but does NOT call
`_cf_delete_gateway_pool(region)` or update the apex LB. The CF origin pool for that region
persists as an orphan, consuming quota.

**Observed:** `appmesh-uw2` pool (from prior gateway-uw2 teardown) blocked creation of
`appmesh-ec1` during Phase 4-A with Cloudflare error 1002:
`"adding 1 origins to current total (2) would surpass the limit (2)"`.

**Impact:** CF plan limit of 2 total origins. Adding a second region fails with origin quota
error, preventing gateway pool creation and making region failover non-functional.

**Fix in app.py:** In `_run_teardown`'s `td_config` step, if `dep.kind == "gateway"`, call
`_cf_delete_gateway_pool(region)` and `_cf_ensure_apex_load_balancer(remaining)`.

**Immediate action:** Manually deleted orphan `appmesh-uw2` pool via CF API.

---

### FIXED: F-008 — `remove_region` doesn't call `_sync_gateway_config()` → S3 config stale

**Repro:** Call `remove_region("eu-central-1")`. The gateway EC2 instances are torn down, DDB
records deleted, CF pool removed, ALB deleted. But `_sync_gateway_config()` is never called,
so the S3 `conf/lb/application.yml` still contains eu-central-1 routes for user services.
The gateway EC2 instances will keep routing to a non-existent eu-central-1 until the S3 config
is updated by the next deploy or teardown.

**Impact:** After removing a region, the gateway's routing table retains stale routes for the
removed region's services for up to 30s per poll cycle, potentially routing requests to dead
endpoints.

**Fix in app.py:** Added `_sync_gateway_config()` call to `remove_region()` after deleting
deployments and before deleting the regional ALB.

---

### DOCUMENTED: F-009 — `http_via_alb` 10s timeout too tight for gateway CB operations

**Repro:** In Phase 4-D, after scaling us-east-1 to 0, all failover attempt requests returned
`None` (timeout) instead of 200 from eu-central-1. Root cause: the gateway takes ~7s to time
out the upstream (connect-timeout 2s + read-timeout 5s), plus ALB overhead, leaving no margin
before the 10s client timeout. In Phase 4 run1, attempt 5 of 6 succeeded; in run2, all 6 of 6
timed out before the circuit breaker opened.

**Fix in `_qa_lib.py`:** Increased `http_via_alb` timeout from 10s to 20s.
**Fix in `_qa_phase4.py`:** Increased failover attempts from 6 to 10 to allow more CB cycles.

---

### FIXED: F-010 — `remove_region` deletes Cloudflare pool before detaching it from apex LB

**Repro:** During Phase 4 region removal, `remove_region("eu-central-1")` deleted the DDB
deployment records, then called `_cf_delete_gateway_pool("eu-central-1")` while that pool was
still referenced by the apex Cloudflare load balancer. Cloudflare returned HTTP 412
`Precondition Failed`, leaving the infra op in `error` and skipping the S3 gateway-config sync.

**Impact:** The region was no longer active in DDB, but `conf/lb/application.yml` still advertised
the removed region for `qa-svc`; the stale `appmesh-ec1` pool also remained attached until manual
cleanup.

**Fix in app.py:** In `remove_region()`, update the apex load balancer with the remaining active
regions before deleting the removed region's pool, then sync the gateway config.

---

### FIXED: F-011 — Teardown retries treat already-deleted resources as failures

**Repro:** Retrying teardown for `qa-svc-p3b-a739a48e` after the underlying AWS/Cloudflare resources
were already gone left the deployment in `teardown_partial`. The failed steps were all desired-state
successes: ASG missing, DNS record 404, launch template missing, IAM role/profile missing, and
security group missing.

**Impact:** Operators can be left with undeletable partial teardown rows even though the real
infrastructure has already been cleaned up.

**Fix in app.py:** Added missing-resource handling in `_run_teardown()` so 404/not-found/already-gone
responses for teardown steps are marked `done`.

---

### FIXED: F-012 — Region onboarding UI loses visible operation state

**Repro:** Start onboarding from `/regions`. The terminal panel can appear briefly, then disappear
after navigation/reconnect, while the target card still looks like a normal "not onboarded" card.

**Impact:** Operators cannot tell which region is currently being onboarded or what step is running.
Repeated clicks can be confusing, and while debugging locally the Flask reloader can reset in-memory
operation state during a real AWS operation.

**Fix:** `/api/regions` now returns operation metadata (`region`, `mode`, `last_line`, log tail).
`regions.html` renders the affected region card as disabled/in-progress with an `onboarding` or
`removing` badge and latest log line. The operation terminal also keeps showing done/error history.
Local `app.py` startup now disables the debug reloader by default; enable it explicitly with
`flask_debug=1` or `FLASK_DEBUG=1` only when not running real AWS operations.

**Verified:** Render test confirms an onboarding region card gets `op-busy`, an `onboarding` badge,
the latest status line, and terminal log content. Live cleanup removed the partial `us-east-2`
region and returned the environment to `us-east-1` + `eu-west-1`.

---

### FIXED: F-013 — Apex landing route breaks when regional gateway origin DNS drifts

**Repro:** `http://landing-us-east-1.appmesh.org/` returned 200, but `http://appmesh.org/`
returned Cloudflare 1016. Cloudflare's `appmesh-ue1` origin pool pointed at
`lb-ue1.appmesh.org`, but that CNAME was missing even though the gateway deployment and shared
ALB still existed.

**Impact:** The landing service and gateway were healthy, but the public apex load balancer had no
resolvable origin, so `appmesh.org/` and `appmesh.org/gateway/status` failed before reaching the
gateway.

**Fix in app.py:** Added gateway-origin DNS reconciliation during Cloudflare apex/pool refresh.
`_cf_ensure_gateway_pool()` now verifies/recreates `lb-<abbrev>.appmesh.org` from the active
gateway deployment's regional ALB before updating the Cloudflare pool.

**Verified:** Reconciliation recreated `lb-ue1.appmesh.org`, public resolvers saw the CNAME, direct
ALB checks with Host `appmesh.org` returned landing HTML, and `http://appmesh.org/` plus
`/gateway/status` returned 200.

---

### FIXED: F-014 — Regional gateways used one shared S3 route config

**Repro:** All gateways read `s3://upb-master-dissertation/conf/lb/application.yml`, so a newly
onboarded gateway had no independent regional route file and the console could not publish a
gateway-specific view of service routes.

**Impact:** Gateway configuration did not match the intended topology where each onboarded region
has its own `application.yml` under `conf/lb/<region>/`. This made region-specific ordering and
operational visibility harder, and it meant new gateway jars depended on the global config object.

**Fix:** The console now writes `conf/lb/<region>/application.yml` for every active gateway region
whenever landing, service deploy, service-region onboard/offboard, import, startup reconciliation, or
region onboard/offboard changes the route table. The gateway jar resolves `{region}` from
`AWS_REGION`/`AWS_DEFAULT_REGION` and reads `conf/lb/<region>/application.yml`. The legacy global
key is still written during migration so old gateway jars continue receiving route updates.

---

## Infrastructure findings (not code bugs)

### INFO: Stale `gateway-uw2` deployment from prior session

Found `gateway-uw2-4c9f3499` (`status=deployed, kind=gateway, region=us-west-2`) in DDB with
2 live t3.small instances in us-west-2. This was from a previous session before the full teardown.
**Action:** Torn down via console teardown API. Verified all resources deleted.

---

## Summary of code changes made to app.py

| Change | File | Line | Description |
|--------|------|------|-------------|
| Added `"kind": "service"` | app.py | 3976 | User-deployed services now have `kind` set in DDB |
| Added UTF-8 stdout reconfigure | app.py | 19-23 | Fixes Windows cp1252 crash on gateway-config sync |
| `_run_scan` rewrite (shared-ALB) | app.py | 2017+ | Fixes phantom group, orphan_dns, broken health, kind=None |
