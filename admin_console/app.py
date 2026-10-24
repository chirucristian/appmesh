from __future__ import annotations

import os
import re
import json
import time
import uuid
import base64
import threading
import sys
import configparser
from pathlib import Path
from functools import wraps
from datetime import datetime, timedelta

# Windows' default cp1252 console can't encode characters we log (→, …, ✓), which
# raises UnicodeEncodeError mid-function (e.g. aborting gateway-config sync or a
# scan thread). Force UTF-8 so the app behaves the same locally as on Linux EC2.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from flask import (
    Flask, render_template, request, redirect,
    url_for, jsonify, Response, stream_with_context, session
)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import boto3
import requests as http_requests
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def load_config():
    path = Path(__file__).parent / "config.properties"
    if not path.exists():
        raise SystemExit(
            f"Missing {path}\n"
            "Copy config.properties.example to config.properties and fill in your "
            "AWS keys, Cloudflare token and root account."
        )
    cfg = configparser.ConfigParser()
    cfg.read(path)
    if "default" not in cfg:
        raise SystemExit(f"{path} has no [default] section — see config.properties.example")
    flat = dict(cfg["default"])
    return flat

CONFIG = load_config()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB upload limit
app.config["SECRET_KEY"] = CONFIG.get("flask_secret_key", "dev-insecure-change-me")
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=int(CONFIG.get("session_hours", "6")))

UPLOAD_FOLDER = Path(__file__).parent / "uploads"
UPLOAD_FOLDER.mkdir(exist_ok=True)

PIPELINES_DIR = Path(__file__).parent / "pipelines"
PIPELINES_DIR.mkdir(exist_ok=True)

# In-memory store for deployments
DEPLOYMENTS: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Local pipeline state files  (fast, reliable, survives restarts)
# ---------------------------------------------------------------------------
def _save_pipeline(dep_id: str, pipeline_type: str, step_keys: list[str],
                   steps: dict, current_step: str = "", status: str = "running"):
    """Write a local pipeline state file so we can resume on restart."""
    state = {
        "type": pipeline_type,
        "dep_id": dep_id,
        "started_at": datetime.utcnow().isoformat(),
        "status": status,
        "current_step": current_step,
        "step_keys": step_keys,
        "completed_steps": [k for k in step_keys if steps.get(k, {}).get("status") == "done"],
    }
    path = PIPELINES_DIR / f"{dep_id}-{pipeline_type}.json"
    try:
        path.write_text(json.dumps(state, indent=2))
    except Exception:
        pass  # best-effort


def _delete_pipeline(dep_id: str, pipeline_type: str):
    """Remove the local pipeline state file (pipeline finished)."""
    path = PIPELINES_DIR / f"{dep_id}-{pipeline_type}.json"
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def _load_pipelines() -> dict[str, dict]:
    """Load all active pipeline state files from disk. Returns {dep_id-type: state}."""
    result = {}
    try:
        for f in PIPELINES_DIR.glob("*.json"):
            try:
                state = json.loads(f.read_text())
                key = f"{state['dep_id']}-{state['type']}"
                result[key] = state
            except Exception:
                continue
    except Exception:
        pass
    return result

# Ordered step keys and labels (shared between thread and SSE)
STEP_KEYS = [
    "create_sg",
    "create_iam_role",
    "upload_s3",
    "resolve_ami",
    "create_launch_template",
    "discover_vpc",
    "ensure_alb",
    "create_target_group",
    "create_listener_rule",
    "create_asg",
    "create_dns_record",
    "wait_app_healthy",
]
STEP_LABELS = {
    "create_sg":               "Creating Security Group (all traffic)",
    "create_iam_role":         "Creating IAM Role & Instance Profile",
    "upload_s3":               "Uploading JAR to S3",
    "resolve_ami":             "Resolving latest AL2023 AMI",
    "create_launch_template":  "Creating EC2 Launch Template",
    "discover_vpc":            "Discovering default VPC & subnets",
    "ensure_alb":              "Ensuring shared regional ALB",
    "create_target_group":     "Creating Target Group",
    "create_listener_rule":    "Adding ALB listener rule (host-based)",
    "create_asg":              "Creating Auto Scaling Group",
    "create_dns_record":       "Creating Cloudflare DNS record",
    "wait_app_healthy":        "Waiting for application to become healthy",
}

# ---------------------------------------------------------------------------
# AWS helpers  (region can be overridden per-deployment)
# ---------------------------------------------------------------------------

def _session(region: str | None = None):
    region_name = region or CONFIG.get("aws_region", "us-east-1")
    access_key = CONFIG.get("aws_access_key_id")
    secret_key = CONFIG.get("aws_secret_access_key")
    if access_key and secret_key:
        return boto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region_name,
        )
    # No static keys (e.g. on EC2) → use the default credential chain (instance role).
    return boto3.Session(region_name=region_name)

def _s3(region: str | None = None):
    # S3 bucket lives in us-east-1 regardless of deployment region
    return _session("us-east-1").client("s3")

def _ec2(region: str | None = None):
    return _session(region).client("ec2")

def _ec2_resource(region: str | None = None):
    return _session(region).resource("ec2")

def _elbv2(region: str | None = None):
    return _session(region).client("elbv2")

def _autoscaling(region: str | None = None):
    return _session(region).client("autoscaling")

def _iam(region: str | None = None):
    return _session(region).client("iam")


S3_CONF_PREFIX = "conf/dashboard/"   # legacy; deployment metadata now lives in DynamoDB

# ---------------------------------------------------------------------------
# DynamoDB:  user accounts  +  deployment metadata
#   - appmesh-users        PK username
#   - appmesh-deployments  PK dep_id, GSI "owner-index" on owner
#   JAR binaries stay in S3; the deployment item stores the S3 pointer in `data`.
# ---------------------------------------------------------------------------
DDB_REGION = CONFIG.get("ddb_region", "us-east-1")
USERS_TABLE = CONFIG.get("ddb_users_table", "appmesh-users")
DEPLOYMENTS_TABLE = CONFIG.get("ddb_deployments_table", "appmesh-deployments")

# Root account credentials come from config.properties (git-ignored), not source.
ROOT_USERNAME = CONFIG.get("root_username", "")
ROOT_PASSWORD = CONFIG.get("root_password", "")


def _ddb_resource():
    return _session(DDB_REGION).resource("dynamodb")


def _ddb_client():
    return _session(DDB_REGION).client("dynamodb")


def _users_table():
    return _ddb_resource().Table(USERS_TABLE)


def _deployments_table():
    return _ddb_resource().Table(DEPLOYMENTS_TABLE)


def _ensure_ddb_tables():
    """Create the users + deployments tables if absent (idempotent)."""
    client = _ddb_client()
    existing = set(client.list_tables().get("TableNames", []))
    if USERS_TABLE not in existing:
        client.create_table(
            TableName=USERS_TABLE,
            KeySchema=[{"AttributeName": "username", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "username", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.get_waiter("table_exists").wait(TableName=USERS_TABLE)
    if DEPLOYMENTS_TABLE not in existing:
        client.create_table(
            TableName=DEPLOYMENTS_TABLE,
            KeySchema=[{"AttributeName": "dep_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "dep_id", "AttributeType": "S"},
                {"AttributeName": "owner", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "owner-index",
                "KeySchema": [{"AttributeName": "owner", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        client.get_waiter("table_exists").wait(TableName=DEPLOYMENTS_TABLE)


# ── User accounts ───────────────────────────────────────────────────
def _seed_root_user():
    """Create the root account if it doesn't exist yet (credentials from config.properties)."""
    if not ROOT_USERNAME or not ROOT_PASSWORD:
        print("WARN: root_username/root_password not set in config.properties — skipping root seed")
        return
    try:
        if not get_user(ROOT_USERNAME):
            _users_table().put_item(Item={
                "username": ROOT_USERNAME,
                "password_hash": generate_password_hash(ROOT_PASSWORD),
                "role": "root",
                "created_at": datetime.utcnow().isoformat(),
            })
    except Exception as e:
        print(f"WARN: could not seed root user: {e}")


def get_user(username: str):
    try:
        return _users_table().get_item(Key={"username": username}).get("Item")
    except Exception:
        return None


def list_users() -> list:
    try:
        return _users_table().scan().get("Items", [])
    except Exception:
        return []


def create_user(username: str, password: str, role: str = "user"):
    _users_table().put_item(
        Item={
            "username": username,
            "password_hash": generate_password_hash(password),
            "role": role,
            "created_at": datetime.utcnow().isoformat(),
        },
        ConditionExpression="attribute_not_exists(username)",
    )


def delete_user(username: str):
    _users_table().delete_item(Key={"username": username})


# ---------------------------------------------------------------------------
# Deployment persistence  (DynamoDB; function names kept for call-site stability)
# ---------------------------------------------------------------------------
def _serializable_dep(dep: dict) -> dict:
    """Return a JSON-safe copy of a deployment dict (strips internal keys)."""
    out = {}
    for k, v in dep.items():
        if k.startswith("_"):
            continue
        if k == "local_path":
            continue  # local path is not useful across restarts
        out[k] = v
    return out


def _save_deployment_to_s3(dep: dict):
    """Persist deployment state as an item in the DynamoDB deployments table."""
    try:
        body = json.dumps(_serializable_dep(dep), default=str)
        _deployments_table().put_item(Item={
            "dep_id": dep["id"],
            "owner": dep.get("owner") or "root",
            "status": dep.get("status", "unknown"),
            "region": dep.get("region", ""),
            "updated_at": datetime.utcnow().isoformat(),
            "data": body,
        })
    except Exception as e:
        print(f"WARN: could not save deployment {dep.get('id')}: {e}")


def _delete_deployment_from_s3(dep_id: str):
    """Remove the deployment item from DynamoDB."""
    try:
        _deployments_table().delete_item(Key={"dep_id": dep_id})
    except Exception:
        pass


def _load_deployments_from_s3():
    """Load all deployments from DynamoDB into DEPLOYMENTS (system-wide view).

    Route-level authorization decides what each user may see/act on.
    In-flight deploys/teardowns held in memory are not overwritten.
    """
    try:
        items = _deployments_table().scan().get("Items", [])
        ddb_ids: set[str] = set()
        for item in items:
            try:
                dep = json.loads(item["data"])
                dep.setdefault("_version", 0)
                dep.setdefault("owner", item.get("owner", "root"))
                # Backfill service-versioning fields for deployments created
                # before the service/history model existed.
                dep.setdefault("version", 1)
                dep.setdefault("version_deployed_at", dep.get("created_at"))
                dep.setdefault("history", [])
                dep.setdefault("health_check_path", "/status")
                if _is_user_service(dep):
                    dep.setdefault("service_name", _safe_service_name(dep.get("name", "")))
                dep_id = dep["id"]
                ddb_ids.add(dep_id)
                existing = DEPLOYMENTS.get(dep_id, {})
                if existing.get("status") in ("deploying", "tearing_down", "updating"):
                    continue
                DEPLOYMENTS[dep_id] = dep
            except Exception:
                continue
        # Drop deployments no longer in the table (unless mid-flight)
        for dep_id in list(DEPLOYMENTS.keys()):
            if dep_id not in ddb_ids:
                if DEPLOYMENTS[dep_id].get("status") not in ("deploying", "tearing_down", "updating"):
                    DEPLOYMENTS.pop(dep_id, None)
    except Exception:
        pass  # if DynamoDB is unreachable, keep current in-memory state


USER_DATA_TEMPLATE = r"""#!/bin/bash
exec > /var/log/user-data.log 2>&1
set -x

# ── 1. Wait for network / metadata service ──────────────
TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" \
        -H "X-aws-ec2-metadata-token-ttl-seconds: 60" || true)
INSTANCE_ID=""
for i in $(seq 1 30); do
  INSTANCE_ID=$(curl -sf -H "X-aws-ec2-metadata-token: $TOKEN" \
       http://169.254.169.254/latest/meta-data/instance-id) && break
  echo "Waiting for metadata service ($i)..."
  sleep 2
done
REGION=$(curl -sf -H "X-aws-ec2-metadata-token: $TOKEN" \
       http://169.254.169.254/latest/meta-data/placement/region || echo "{aws_region}")

# ── 2. Install packages (don't let dnf update failure kill the script) ──
dnf -y update || true
dnf -y install java-21-amazon-corretto unzip

# ── 3. Install AWS CLI v2 ───────────────────────────────
if ! command -v aws &>/dev/null; then
  curl -sL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
  unzip -qo /tmp/awscliv2.zip -d /tmp
  /tmp/aws/install
  rm -rf /tmp/awscliv2.zip /tmp/aws
fi

# ── 3b. Tag this instance with a unique name ─────────────
SHORT_UID=$(cat /proc/sys/kernel/random/uuid | cut -d'-' -f1)
INSTANCE_NAME="{service_name}-{deploy_uid}-$SHORT_UID"
for i in $(seq 1 6); do
  aws ec2 create-tags --region "$REGION" \
      --resources "$INSTANCE_ID" \
      --tags "Key=Name,Value=$INSTANCE_NAME" && break
  echo "Tagging attempt $i failed, retrying in 10s..."
  sleep 10
done
echo "Instance tagged as: $INSTANCE_NAME"

# ── 4. Download JAR from S3 (retry up to 2 minutes) ─────
mkdir -p /app
DOWNLOADED=false
for i in $(seq 1 12); do
  if aws s3 cp "{s3_uri}" /app/{jar_filename}; then
    DOWNLOADED=true
    break
  fi
  echo "S3 download attempt $i failed, retrying in 10s..."
  sleep 10
done

if [ "$DOWNLOADED" != "true" ]; then
  echo "FATAL: could not download JAR after 12 attempts"
  exit 1
fi

# ── 5. Create and start systemd service ──────────────────
cat >/etc/systemd/system/myapp.service <<'EOF'
[Unit]
Description=My Java App
After=network.target

[Service]
Type=simple
WorkingDirectory=/app
Environment=AWS_REGION={aws_region}
ExecStart=/usr/bin/java -jar /app/{jar_filename}
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable myapp
systemctl restart myapp
"""

EC2_ASSUME_ROLE_POLICY = json.dumps({
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "ec2.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
})


def _get_default_vpc_and_subnets(region: str | None = None):
    """Return (vpc_id, [subnet_ids], [az_names]) for the default VPC (2 AZs)."""
    ec2 = _ec2(region)
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])
    vpc_id = vpcs["Vpcs"][0]["VpcId"]

    sn_resp = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
    # Pick one subnet per AZ to guarantee distinct AZs (ALB requires ≥2)
    seen_azs: dict[str, dict] = {}
    for sn in sn_resp["Subnets"]:
        az = sn["AvailabilityZone"]
        if az not in seen_azs:
            seen_azs[az] = sn
    if len(seen_azs) < 2:
        raise RuntimeError(
            f"Need subnets in at least 2 AZs but found {len(seen_azs)} in VPC {vpc_id}"
        )
    picked = list(seen_azs.values())[:2]
    subnet_ids = [s["SubnetId"] for s in picked]
    azs = [s["AvailabilityZone"] for s in picked]
    return vpc_id, subnet_ids, azs


def _get_latest_al2023_ami(region: str | None = None):
    """Get the latest Amazon Linux 2023 AMI."""
    ec2 = _ec2(region)
    resp = ec2.describe_images(
        Owners=["amazon"],
        Filters=[
            {"Name": "name", "Values": ["al2023-ami-2023*-x86_64"]},
            {"Name": "state", "Values": ["available"]},
        ],
    )
    images = sorted(resp["Images"], key=lambda x: x["CreationDate"], reverse=True)
    return images[0]["ImageId"]


# ---------------------------------------------------------------------------
# Cloudflare DNS helpers
# ---------------------------------------------------------------------------
CF_API_BASE = "https://api.cloudflare.com/client/v4"


def _cf_headers():
    return {
        "Authorization": f"Bearer {CONFIG.get('cloudflare_api_token', '')}",
        "Content-Type": "application/json",
    }


def _cloudflare_enabled() -> bool:
    """Cloudflare DNS is skipped entirely when `skip_cloudflare` is set (or creds
    are absent). With it off, the AWS host-header rules are still created; only the
    public DNS records are not — point DNS at the regional ALB manually later."""
    if str(CONFIG.get("skip_cloudflare", "")).strip().lower() in ("1", "true", "yes"):
        return False
    return bool(CONFIG.get("cloudflare_api_token") and CONFIG.get("cloudflare_zone_id"))


def _cf_dns_name(dep_name: str, region: str | None = None) -> str:
    """Build the CNAME record name: <service_name>-<region>.appmesh.org"""
    region = region or CONFIG.get("aws_region", "us-east-1")
    domain = CONFIG.get("cloudflare_domain", "appmesh.org")
    # Sanitise service name for DNS (lowercase, only a-z 0-9 -)
    safe_name = re.sub(r"[^a-z0-9\-]", "-", dep_name.lower()).strip("-")
    return f"{safe_name}-{region}.{domain}"


def _safe_service_name(dep_name: str) -> str:
    """The service's URL prefix / gateway map key (lowercase, a-z 0-9 -)."""
    return re.sub(r"[^a-z0-9\-]", "-", dep_name.lower()).strip("-")


def _is_user_service(dep: dict) -> bool:
    return dep.get("kind") not in ("gateway", "console", "landing")


def _service_key(dep: dict) -> str:
    return _safe_service_name(dep.get("service_name") or dep.get("name", ""))


def _service_deployments(service_name: str, owner: str | None = None,
                         include_inactive: bool = False) -> list[dict]:
    key = _safe_service_name(service_name)
    out = []
    for dep in DEPLOYMENTS.values():
        if not _is_user_service(dep):
            continue
        if _service_key(dep) != key:
            continue
        if owner and (dep.get("owner") or "root") != owner:
            continue
        if not include_inactive and dep.get("status") in ("torn_down", "teardown_partial"):
            continue
        out.append(dep)
    return sorted(out, key=lambda d: d.get("region", ""))


def _service_regions(service_name: str, owner: str | None = None) -> set[str]:
    return {d.get("region") for d in _service_deployments(service_name, owner) if d.get("region")}


def _service_summary(dep: dict) -> dict:
    siblings = _service_deployments(_service_key(dep), dep.get("owner") if not is_root() else None)
    regions = [d.get("region") for d in siblings if d.get("region")]
    available = [r for r in _load_active_regions() if r not in regions]
    return {"siblings": siblings, "regions": regions, "available_regions": available}


# ---------------------------------------------------------------------------
# Gateway routing config  (console → s3://<bucket>/conf/lb/<region>/application.yml)
#
# Users reach a service at  appmesh.org/<service>/<path>.  Cloudflare sends the
# request to the nearest regional gateway; the gateway matches the first path
# segment, strips it, and proxies the rest to one of the service's regional
# backends (http://<service>-<region>.appmesh.org → that region's shared ALB).
#
# Each gateway reads its own regional object from S3 and re-polls it every 30s,
# so whenever a service is deployed or torn down we regenerate one application.yml
# per onboarded gateway region from the live deployment set.
# ---------------------------------------------------------------------------
GATEWAY_CONFIG_PREFIX = "conf/lb"
# Legacy single-object key (conf/lb/application.yml). No longer written — every
# regional gateway now reads its own conf/lb/<region>/application.yml (route-sync.key
# = conf/lb/{region}/application.yml). Kept only so teardown can delete a stale
# object left behind by older builds.
GATEWAY_CONFIG_LEGACY_KEY = f"{GATEWAY_CONFIG_PREFIX}/application.yml"
# Failover preference order (primary first); unknown regions sort after, alphabetically.
GATEWAY_REGION_ORDER = ["us-east-1", "eu-central-1", "eu-west-1"]


def _gateway_config_key(region: str) -> str:
    return f"{GATEWAY_CONFIG_PREFIX}/{region}/application.yml"


def _build_gateway_services() -> dict:
    """Aggregate live, deployed services into the gateway's service map:
        { service_name: {"paths": [str], "health_check_path": str, "regions": {region: url}} }
    Gateways and the console are infrastructure, not routable, so they are excluded.
    The landing service IS included but mapped to the root path "/" (it backs
    appmesh.org/); user services are mapped to "/<name>/**". Only fully `deployed`
    services are routed.
    """
    services: dict[str, dict] = {}
    for dep in DEPLOYMENTS.values():
        kind = dep.get("kind")
        if kind in ("gateway", "console"):
            continue
        if dep.get("status") != "deployed":
            continue
        name = _service_key(dep)
        region = dep.get("region") or CONFIG.get("aws_region", "us-east-1")
        paths = ["/"] if kind == "landing" else [f"/{name}/**"]
        svc = services.setdefault(name, {
            "paths": paths,
            "health_check_path": dep.get("health_check_path") or "/status",
            "regions": {},
        })
        svc["regions"][region] = f"http://{_cf_dns_name(name, region)}"
    return services


def _ordered_gateway_regions(regions: dict, gateway_region: str | None = None) -> list[str]:
    ordered: list[str] = []
    if gateway_region and gateway_region in regions:
        ordered.append(gateway_region)
    ordered += [r for r in GATEWAY_REGION_ORDER if r in regions and r not in ordered]
    ordered += [r for r in sorted(regions) if r not in ordered]
    return ordered


def _render_gateway_yaml(services: dict, gateway_region: str | None = None) -> str:
    """Render the gateway `application.yml` (kebab-case keys, parsed by Jackson)."""
    source = "all gateway regions" if not gateway_region else f"gateway region: {gateway_region}"
    lines = [
        "# ============================================================",
        "#  Generated by the AppMesh console - DO NOT EDIT BY HAND.",
        "#  Source of truth: the console's DynamoDB deployment table.",
        f"#  {source}",
        "# ============================================================",
        "gateway:",
    ]
    if not services:
        lines.append("  services: {}")
    else:
        lines.append("  services:")
        for name in sorted(services):
            svc = services[name]
            regions = svc["regions"]
            ordered = _ordered_gateway_regions(regions, gateway_region)
            lines.append(f"    {name}:")
            lines.append("      paths:")
            for p in svc.get("paths") or [f"/{name}/**"]:
                lines.append(f"        - {p}")
            lines.append(f"      health-check-path: {svc['health_check_path']}")
            lines.append("      regions:")
            for r in ordered:
                lines.append(f"        - name: {r}")
                lines.append(f"          url: {regions[r]}")
    has_multi_region = any(len((svc.get("regions") or {})) > 1 for svc in services.values())
    if has_multi_region:
        lines += [
            "  circuit-breaker:",
            "    failure-threshold: 3",
            "    success-threshold: 2",
            "    half-open-delay-seconds: 30",
        ]
    lines += [
        "  health-check:",
        "    interval-seconds: 15",
        "    timeout-seconds: 5",
        "  timeout:",
        "    connect-timeout-ms: 2000",
        "    read-timeout-ms: 5000",
        "",
    ]
    return "\n".join(lines)


def _delete_gateway_config(region: str, log=print):
    """Remove a regional gateway config object after the region is offboarded."""
    try:
        _s3().delete_object(Bucket=CONFIG["s3_bucket"], Key=_gateway_config_key(region))
        log(f"INFO: gateway config deleted for {region}")
    except Exception as e:
        log(f"WARN: could not delete gateway config for {region}: {e}")


def _sync_gateway_config(log=print):
    """Regenerate the per-region gateway application.yml files in S3.

    One object per onboarded gateway region (conf/lb/<region>/application.yml);
    each regional gateway polls only its own object and picks up the change on its
    next poll (<=30s). Best-effort: a failure here must never break a deploy or
    teardown, so it only logs.
    """
    try:
        services = _build_gateway_services()
        active_regions = _load_active_regions()
        for region in active_regions:
            _s3().put_object(
                Bucket=CONFIG["s3_bucket"],
                Key=_gateway_config_key(region),
                Body=_render_gateway_yaml(services, gateway_region=region).encode("utf-8"),
                ContentType="text/yaml",
            )
        names = ", ".join(sorted(services)) or "(none)"
        region_names = ", ".join(active_regions) or "(no onboarded gateway regions)"
        log(f"INFO: gateway config synced → {len(services)} service(s): {names}; regions: {region_names}")
    except Exception as e:
        log(f"WARN: could not sync gateway config: {e}")


def _cf_find_record(zone_id: str, fqdn: str):
    """Return the Cloudflare DNS record dict if it exists, else None."""
    if not _cloudflare_enabled():
        return None
    url = f"{CF_API_BASE}/zones/{zone_id}/dns_records"
    resp = http_requests.get(url, headers=_cf_headers(), params={"type": "CNAME", "name": fqdn})
    resp.raise_for_status()
    records = resp.json().get("result", [])
    for r in records:
        if r["name"] == fqdn:
            return r
    return None


def _cf_create_record(zone_id: str, fqdn: str, target: str) -> dict:
    """Create a CNAME record. Returns the created record dict."""
    if not _cloudflare_enabled():
        return {"id": ""}
    url = f"{CF_API_BASE}/zones/{zone_id}/dns_records"
    payload = {
        "type": "CNAME",
        "name": fqdn,
        "content": target,
        "ttl": 1,       # 1 = automatic
        "proxied": False,  # DNS-only so ALB health checks work directly
    }
    resp = http_requests.post(url, headers=_cf_headers(), json=payload)
    resp.raise_for_status()
    body = resp.json()
    if not body.get("success"):
        raise RuntimeError(f"Cloudflare API error: {body.get('errors', body)}")
    return body["result"]


def _cf_update_record(zone_id: str, record_id: str, fqdn: str, target: str) -> dict:
    """Repoint an existing CNAME at `target` (e.g. after the ALB was recreated)."""
    if not _cloudflare_enabled():
        return {"id": record_id}
    url = f"{CF_API_BASE}/zones/{zone_id}/dns_records/{record_id}"
    payload = {"type": "CNAME", "name": fqdn, "content": target, "ttl": 1, "proxied": False}
    resp = http_requests.put(url, headers=_cf_headers(), json=payload)
    resp.raise_for_status()
    body = resp.json()
    if not body.get("success"):
        raise RuntimeError(f"Cloudflare update error: {body.get('errors', body)}")
    return body["result"]


def _cf_delete_record(zone_id: str, record_id: str):
    """Delete a DNS record by ID."""
    if not _cloudflare_enabled() or not record_id:
        return
    url = f"{CF_API_BASE}/zones/{zone_id}/dns_records/{record_id}"
    resp = http_requests.delete(url, headers=_cf_headers())
    resp.raise_for_status()
    body = resp.json()
    if not body.get("success"):
        raise RuntimeError(f"Cloudflare delete error: {body.get('errors', body)}")


# ---------------------------------------------------------------------------
# Shared regional ALB helpers
#   One ALB per region (named "<alb_name_prefix><region>") fronts every service
#   via host-header listener rules → per-service target groups. Reused by both
#   the console deploy worker and initialize_infrastructure.py.
# ---------------------------------------------------------------------------
ALB_NAME_PREFIX = CONFIG.get("alb_name_prefix", "appmesh-alb-")


def _regional_alb_name(region: str) -> str:
    return f"{ALB_NAME_PREFIX}{region}"


def _ensure_regional_alb_sg(region: str, vpc_id: str) -> str:
    """Return the id of the region's shared-ALB security group (port 80), creating if absent."""
    ec2 = _ec2(region)
    sg_name = f"appmesh-alb-sg-{region}"
    try:
        resp = ec2.describe_security_groups(Filters=[
            {"Name": "group-name", "Values": [sg_name]},
            {"Name": "vpc-id", "Values": [vpc_id]},
        ])
        if resp["SecurityGroups"]:
            return resp["SecurityGroups"][0]["GroupId"]
    except ClientError:
        pass
    sg_id = ec2.create_security_group(
        GroupName=sg_name, Description="AppMesh shared regional ALB", VpcId=vpc_id,
    )["GroupId"]
    ec2.authorize_security_group_ingress(GroupId=sg_id, IpPermissions=[{
        "IpProtocol": "tcp", "FromPort": 80, "ToPort": 80,
        "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
    }])
    return sg_id


def _create_default_listener(elbv2, alb_arn: str) -> str:
    """Create the HTTP:80 listener with a default 404 (per-service rules are added later)."""
    resp = elbv2.create_listener(
        LoadBalancerArn=alb_arn, Protocol="HTTP", Port=80,
        DefaultActions=[{
            "Type": "fixed-response",
            "FixedResponseConfig": {
                "StatusCode": "404", "ContentType": "text/plain",
                "MessageBody": "No matching service",
            },
        }],
    )
    return resp["Listeners"][0]["ListenerArn"]


def _ensure_regional_alb(region: str, subnet_ids=None):
    """Return (alb_arn, alb_dns, listener_arn) for the region's shared ALB, creating if absent."""
    elbv2 = _elbv2(region)
    alb_name = _regional_alb_name(region)
    try:
        alb = elbv2.describe_load_balancers(Names=[alb_name])["LoadBalancers"][0]
        alb_arn = alb["LoadBalancerArn"]
        alb_dns = alb["DNSName"]
        listeners = elbv2.describe_listeners(LoadBalancerArn=alb_arn)["Listeners"]
        listener_arn = listeners[0]["ListenerArn"] if listeners else _create_default_listener(elbv2, alb_arn)
        return alb_arn, alb_dns, listener_arn
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("LoadBalancerNotFound", "ValidationError"):
            raise

    vpc_id, default_subnets, _azs = _get_default_vpc_and_subnets(region)
    if not subnet_ids:
        subnet_ids = default_subnets
    alb_sg = _ensure_regional_alb_sg(region, vpc_id)
    alb = elbv2.create_load_balancer(
        Name=alb_name, Subnets=subnet_ids, SecurityGroups=[alb_sg],
        Scheme="internet-facing", Type="application", IpAddressType="ipv4",
    )["LoadBalancers"][0]
    alb_arn = alb["LoadBalancerArn"]
    alb_dns = alb["DNSName"]
    elbv2.get_waiter("load_balancer_available").wait(LoadBalancerArns=[alb_arn])
    listener_arn = _create_default_listener(elbv2, alb_arn)
    return alb_arn, alb_dns, listener_arn


def _next_rule_priority(elbv2, listener_arn: str) -> int:
    """Return the next free numeric listener-rule priority."""
    rules = elbv2.describe_rules(ListenerArn=listener_arn)["Rules"]
    used = [int(r["Priority"]) for r in rules if r.get("Priority", "default").isdigit()]
    return (max(used) + 1) if used else 1


def _add_host_rule(region: str, listener_arn: str, host: str, target_group_arn: str) -> str:
    """Add a host-header rule forwarding `host` to the target group. Returns the rule ARN."""
    elbv2 = _elbv2(region)
    priority = _next_rule_priority(elbv2, listener_arn)
    rule = elbv2.create_rule(
        ListenerArn=listener_arn,
        Priority=priority,
        Conditions=[{"Field": "host-header", "HostHeaderConfig": {"Values": [host]}}],
        Actions=[{"Type": "forward", "TargetGroupArn": target_group_arn}],
    )["Rules"][0]
    return rule["RuleArn"]


# ---------------------------------------------------------------------------
# Teardown steps & labels
# ---------------------------------------------------------------------------
UPDATE_STEP_KEYS = [
    "update_upload_s3",
    "update_launch_template",
    "update_asg_refresh",
    "update_wait_refresh",
    "update_wait_healthy",
]
UPDATE_STEP_LABELS = {
    "update_upload_s3":         "Uploading new JAR to S3",
    "update_launch_template":   "Creating new Launch Template version",
    "update_asg_refresh":       "Starting ASG instance refresh",
    "update_wait_refresh":      "Waiting for instance refresh to complete",
    "update_wait_healthy":      "Waiting for application to become healthy",
}


TEARDOWN_STEP_KEYS = [
    "td_dns_record",
    "td_asg",
    "td_listener_rule",
    "td_target_group",
    "td_launch_template",
    "td_instance_profile",
    "td_iam_role",
    "td_s3_jar",
    "td_security_group",
    "td_config",
]
TEARDOWN_STEP_LABELS = {
    "td_dns_record":       "Deleting Cloudflare DNS record",
    "td_asg":              "Deleting Auto Scaling Group",
    "td_listener_rule":    "Deleting ALB listener rule",
    "td_target_group":     "Deleting Target Group",
    "td_launch_template":  "Deleting Launch Template",
    "td_instance_profile": "Deleting Instance Profile",
    "td_iam_role":         "Deleting IAM Role",
    "td_s3_jar":           "Deleting JAR from S3",
    "td_security_group":   "Deleting Security Group",
    "td_config":           "Removing deployment config",
}


# ---------------------------------------------------------------------------
# Shared teardown logic (simple, used by deploy rollback)
# ---------------------------------------------------------------------------
def _teardown_resources_simple(dep: dict) -> list[str]:
    """Delete all AWS resources recorded in dep['resources']. Returns list of errors."""
    res = dep.get("resources", {})
    region = dep.get("region") or CONFIG.get("aws_region", "us-east-1")
    errors = []

    # Cloudflare DNS record
    try:
        if res.get("cloudflare_record_id"):
            cf_zone_id = CONFIG.get("cloudflare_zone_id", "")
            _cf_delete_record(cf_zone_id, res["cloudflare_record_id"])
    except Exception as e:
        errors.append(f"DNS: {e}")

    # Apex appmesh.org record (only the primary gateway has it)
    try:
        if res.get("apex_record_id"):
            _cf_delete_record(CONFIG.get("cloudflare_zone_id", ""), res["apex_record_id"])
    except Exception as e:
        errors.append(f"Apex DNS: {e}")

    try:
        if res.get("asg_name"):
            asg_client = _autoscaling(region)
            # Terminate instances immediately to skip connection draining
            try:
                asg_resp = asg_client.describe_auto_scaling_groups(
                    AutoScalingGroupNames=[res["asg_name"]]
                )
                if asg_resp["AutoScalingGroups"]:
                    ids = [i["InstanceId"] for i in asg_resp["AutoScalingGroups"][0].get("Instances", [])]
                    if ids:
                        _ec2(region).terminate_instances(InstanceIds=ids)
            except Exception:
                pass
            asg_client.delete_auto_scaling_group(
                AutoScalingGroupName=res["asg_name"], ForceDelete=True
            )
    except Exception as e:
        errors.append(f"ASG: {e}")

    try:
        # Delete only this service's listener rule — the regional ALB is shared.
        if res.get("listener_rule_arn"):
            _elbv2(region).delete_rule(RuleArn=res["listener_rule_arn"])
    except Exception as e:
        errors.append(f"Listener rule: {e}")

    try:
        # Apex appmesh.org listener rule (only the primary gateway has it)
        if res.get("apex_rule_arn"):
            _elbv2(region).delete_rule(RuleArn=res["apex_rule_arn"])
    except Exception as e:
        errors.append(f"Apex rule: {e}")

    try:
        if res.get("target_group_arn"):
            time.sleep(5)
            _elbv2(region).delete_target_group(TargetGroupArn=res["target_group_arn"])
    except Exception as e:
        errors.append(f"TG: {e}")

    try:
        if res.get("launch_template_id"):
            _ec2(region).delete_launch_template(LaunchTemplateId=res["launch_template_id"])
    except Exception as e:
        errors.append(f"LT: {e}")

    try:
        if res.get("instance_profile_name"):
            iam = _iam(region)
            role_name = res.get("iam_role_name", "")
            if role_name:
                try:
                    iam.remove_role_from_instance_profile(
                        InstanceProfileName=res["instance_profile_name"],
                        RoleName=role_name,
                    )
                except Exception:
                    pass
            iam.delete_instance_profile(InstanceProfileName=res["instance_profile_name"])
    except Exception as e:
        errors.append(f"Instance Profile: {e}")

    try:
        if res.get("iam_role_name"):
            iam = _iam(region)
            try:
                iam.detach_role_policy(
                    RoleName=res["iam_role_name"],
                    PolicyArn="arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess",
                )
            except Exception:
                pass
            try:
                iam.delete_role_policy(
                    RoleName=res["iam_role_name"],
                    PolicyName="ec2-self-tag",
                )
            except Exception:
                pass
            iam.delete_role(RoleName=res["iam_role_name"])
    except Exception as e:
        errors.append(f"IAM Role: {e}")

    try:
        if res.get("s3_uri"):
            s3_key = res.get("s3_key") or (CONFIG.get("s3_prefix", "jars/") + dep["jar_filename"])
            _s3().delete_object(Bucket=CONFIG["s3_bucket"], Key=s3_key)
    except Exception as e:
        errors.append(f"S3: {e}")

    try:
        if res.get("security_group_id"):
            time.sleep(5)
            _ec2(region).delete_security_group(GroupId=res["security_group_id"])
    except Exception as e:
        errors.append(f"SG: {e}")

    return errors


# ---------------------------------------------------------------------------
# Background teardown runner (step-by-step with progress)
# ---------------------------------------------------------------------------
def _run_teardown(dep_id: str):
    """Runs in a background thread. Updates dep['teardown_steps'] in-place."""
    dep = DEPLOYMENTS[dep_id]
    res = dep.get("resources", {})
    region = dep.get("region") or CONFIG.get("aws_region", "us-east-1")

    _save_pipeline(dep_id, "teardown", TEARDOWN_STEP_KEYS,
                   dep.get("teardown_steps", {}), status="running")

    def _already_done(step):
        """Return True if this step was already completed (e.g. before a restart)."""
        return dep.get("teardown_steps", {}).get(step, {}).get("status") == "done"

    def mark(step, status, detail=""):
        dep["teardown_steps"][step] = {"status": status, "detail": detail, "label": TEARDOWN_STEP_LABELS[step]}
        dep["_td_version"] = dep.get("_td_version", 0) + 1
        # Don't re-save to S3 after the config deletion step (it would re-create the file)
        if step != "td_config":
            _save_deployment_to_s3(dep)
        if status == "done":
            _save_pipeline(dep_id, "teardown", TEARDOWN_STEP_KEYS,
                           dep.get("teardown_steps", {}), current_step=step, status="running")
            time.sleep(2)  # pause between steps so AWS resources settle

    def _missing_resource(exc, *codes):
        code = ""
        if isinstance(exc, ClientError):
            code = exc.response.get("Error", {}).get("Code", "")
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        msg = str(exc).lower()
        return (
            code in codes
            or status_code == 404
            or "not found" in msg
            or "cannot be found" in msg
            or "does not exist" in msg
        )

    errors = []

    def _wait_gone(check_fn, label, timeout=120):
        """Poll check_fn(); return when it raises ClientError (NotFound) or timeout."""
        for _ in range(timeout // 3):
            try:
                check_fn()
                time.sleep(3)
            except ClientError:
                return  # resource is gone
        raise RuntimeError(f"{label} still exists after {timeout}s")

    try:
        # 0 — Cloudflare DNS record
        if _already_done("td_dns_record"):
            pass  # skip — completed before restart
        else:
            mark("td_dns_record", "running")
            if res.get("cloudflare_record_id") and res.get("dns_fqdn"):
                try:
                    cf_zone_id = CONFIG.get("cloudflare_zone_id", "")
                    _cf_delete_record(cf_zone_id, res["cloudflare_record_id"])
                    for _poll in range(10):
                        check = _cf_find_record(cf_zone_id, res["dns_fqdn"])
                        if not check:
                            break
                        time.sleep(2)
                    mark("td_dns_record", "done",
                         f"Deleted & confirmed gone: {res['dns_fqdn']}")
                except Exception as e:
                    if _missing_resource(e):
                        mark("td_dns_record", "done", f"Already gone: {res['dns_fqdn']}")
                    else:
                        errors.append(str(e))
                        mark("td_dns_record", "failed", str(e))
            else:
                mark("td_dns_record", "done", "Skipped — not created")
            # Apex appmesh.org record (primary gateway only)
            if res.get("apex_record_id"):
                try:
                    _cf_delete_record(CONFIG.get("cloudflare_zone_id", ""), res["apex_record_id"])
                except Exception as e:
                    errors.append(f"Apex DNS: {e}")

        # 1 — ASG (terminate all instances immediately, skip connection draining)
        if _already_done("td_asg"):
            pass
        else:
            mark("td_asg", "running")
            if res.get("asg_name"):
                try:
                    asg_client = _autoscaling(region)
                    ec2_td = _ec2(region)
                    asg_resp = asg_client.describe_auto_scaling_groups(
                        AutoScalingGroupNames=[res["asg_name"]]
                    )
                    instance_ids = []
                    if asg_resp["AutoScalingGroups"]:
                        instance_ids = [
                            i["InstanceId"]
                            for i in asg_resp["AutoScalingGroups"][0].get("Instances", [])
                        ]
                    asg_client.delete_auto_scaling_group(
                        AutoScalingGroupName=res["asg_name"], ForceDelete=True
                    )
                    if instance_ids:
                        ec2_td.terminate_instances(InstanceIds=instance_ids)
                    for _i in range(40):
                        resp = asg_client.describe_auto_scaling_groups(
                            AutoScalingGroupNames=[res["asg_name"]]
                        )
                        if not resp["AutoScalingGroups"]:
                            break
                        time.sleep(15)
                    detail_msg = f"Deleted & confirmed gone: {res['asg_name']}"
                    if instance_ids:
                        detail_msg += f" ({len(instance_ids)} instances terminated)"
                    mark("td_asg", "done", detail_msg)
                except Exception as e:
                    if _missing_resource(e, "ValidationError"):
                        mark("td_asg", "done", f"Already gone: {res['asg_name']}")
                    else:
                        errors.append(str(e))
                        mark("td_asg", "failed", str(e))
            else:
                mark("td_asg", "done", "Skipped — not created")

        # 2 — Listener rule (the regional ALB itself is shared and left in place)
        if _already_done("td_listener_rule"):
            pass
        else:
            mark("td_listener_rule", "running")
            if res.get("listener_rule_arn"):
                try:
                    _elbv2(region).delete_rule(RuleArn=res["listener_rule_arn"])
                    mark("td_listener_rule", "done", "Deleted listener rule")
                except Exception as e:
                    errors.append(str(e))
                    mark("td_listener_rule", "failed", str(e))
            else:
                mark("td_listener_rule", "done", "Skipped — not created")
            # Apex appmesh.org listener rule (primary gateway only)
            if res.get("apex_rule_arn"):
                try:
                    _elbv2(region).delete_rule(RuleArn=res["apex_rule_arn"])
                except Exception as e:
                    errors.append(f"Apex rule: {e}")

        # 4 — Target Group (must wait for ALB to release it)
        if _already_done("td_target_group"):
            pass
        else:
            mark("td_target_group", "running")
            if res.get("target_group_arn"):
                try:
                    elbv2 = _elbv2(region)
                    for _attempt in range(10):
                        try:
                            elbv2.delete_target_group(TargetGroupArn=res["target_group_arn"])
                            break
                        except ClientError as ce:
                            if "is currently in use" in str(ce):
                                time.sleep(5)
                            else:
                                raise
                    _wait_gone(
                        lambda: elbv2.describe_target_groups(TargetGroupArns=[res["target_group_arn"]]),
                        "Target Group",
                    )
                    mark("td_target_group", "done", f"Deleted & confirmed gone: {res.get('target_group_name', '')}")
                except Exception as e:
                    errors.append(str(e))
                    mark("td_target_group", "failed", str(e))
            else:
                mark("td_target_group", "done", "Skipped — not created")

        # 5 — Launch Template
        if _already_done("td_launch_template"):
            pass
        else:
            mark("td_launch_template", "running")
            if res.get("launch_template_id"):
                try:
                    ec2 = _ec2(region)
                    ec2.delete_launch_template(LaunchTemplateId=res["launch_template_id"])
                    _wait_gone(
                        lambda: ec2.describe_launch_templates(LaunchTemplateIds=[res["launch_template_id"]]),
                        "Launch Template",
                    )
                    mark("td_launch_template", "done", f"Deleted & confirmed gone: {res.get('launch_template_name', '')}")
                except Exception as e:
                    if _missing_resource(e, "InvalidLaunchTemplateId.NotFound"):
                        mark("td_launch_template", "done", f"Already gone: {res.get('launch_template_name', '')}")
                    else:
                        errors.append(str(e))
                        mark("td_launch_template", "failed", str(e))
            else:
                mark("td_launch_template", "done", "Skipped — not created")

        # 6 — Instance Profile
        if _already_done("td_instance_profile"):
            pass
        else:
            mark("td_instance_profile", "running")
            if res.get("instance_profile_name"):
                try:
                    iam = _iam(region)
                    role_name = res.get("iam_role_name", "")
                    if role_name:
                        try:
                            iam.remove_role_from_instance_profile(
                                InstanceProfileName=res["instance_profile_name"],
                                RoleName=role_name,
                            )
                        except ClientError:
                            pass
                    try:
                        iam.delete_instance_profile(InstanceProfileName=res["instance_profile_name"])
                    except ClientError as ce:
                        if ce.response["Error"]["Code"] not in ("NoSuchEntity", "NoSuchEntityException"):
                            raise
                    _wait_gone(
                        lambda: iam.get_instance_profile(InstanceProfileName=res["instance_profile_name"]),
                        "Instance Profile",
                    )
                    mark("td_instance_profile", "done", f"Deleted & confirmed gone: {res['instance_profile_name']}")
                except Exception as e:
                    if _missing_resource(e, "NoSuchEntity", "NoSuchEntityException"):
                        mark("td_instance_profile", "done", f"Already gone: {res['instance_profile_name']}")
                    else:
                        errors.append(str(e))
                        mark("td_instance_profile", "failed", str(e))
            else:
                mark("td_instance_profile", "done", "Skipped — not created")

        # 7 — IAM Role
        if _already_done("td_iam_role"):
            pass
        else:
            mark("td_iam_role", "running")
            if res.get("iam_role_name"):
                try:
                    iam = _iam(region)
                    try:
                        iam.detach_role_policy(
                            RoleName=res["iam_role_name"],
                            PolicyArn="arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess",
                        )
                    except ClientError:
                        pass
                    try:
                        iam.delete_role_policy(
                            RoleName=res["iam_role_name"],
                            PolicyName="ec2-self-tag",
                        )
                    except ClientError:
                        pass
                    try:
                        iam.delete_role(RoleName=res["iam_role_name"])
                    except ClientError as ce:
                        if ce.response["Error"]["Code"] not in ("NoSuchEntity", "NoSuchEntityException"):
                            raise
                    _wait_gone(
                        lambda: iam.get_role(RoleName=res["iam_role_name"]),
                        "IAM Role",
                    )
                    mark("td_iam_role", "done", f"Deleted & confirmed gone: {res['iam_role_name']}")
                except Exception as e:
                    if _missing_resource(e, "NoSuchEntity", "NoSuchEntityException"):
                        mark("td_iam_role", "done", f"Already gone: {res['iam_role_name']}")
                    else:
                        errors.append(str(e))
                        mark("td_iam_role", "failed", str(e))
            else:
                mark("td_iam_role", "done", "Skipped — not created")

        # 8 — S3 JAR
        if _already_done("td_s3_jar"):
            pass
        else:
            mark("td_s3_jar", "running")
            if res.get("s3_uri"):
                try:
                    s3 = _s3()
                    s3_key = res.get("s3_key") or (CONFIG.get("s3_prefix", "jars/") + dep["jar_filename"])
                    s3.delete_object(Bucket=CONFIG["s3_bucket"], Key=s3_key)
                    _wait_gone(
                        lambda: s3.head_object(Bucket=CONFIG["s3_bucket"], Key=s3_key),
                        "S3 JAR",
                    )
                    mark("td_s3_jar", "done", f"Deleted & confirmed gone: {res['s3_uri']}")
                except Exception as e:
                    errors.append(str(e))
                    mark("td_s3_jar", "failed", str(e))
            else:
                mark("td_s3_jar", "done", "Skipped — not uploaded")

        # 9 — Security Group (must wait for ENIs to fully detach)
        if _already_done("td_security_group"):
            pass
        else:
            mark("td_security_group", "running")
            if res.get("security_group_id"):
                try:
                    ec2 = _ec2(region)
                    for _attempt in range(20):
                        try:
                            ec2.delete_security_group(GroupId=res["security_group_id"])
                            break
                        except ClientError as ce:
                            if "DependencyViolation" in str(ce):
                                time.sleep(10)
                            else:
                                raise
                    _wait_gone(
                        lambda: ec2.describe_security_groups(GroupIds=[res["security_group_id"]]),
                        "Security Group",
                    )
                    mark("td_security_group", "done",
                         f"Deleted & confirmed gone: {res.get('security_group_name', '')} ({res['security_group_id']})")
                except Exception as e:
                    if _missing_resource(e, "InvalidGroup.NotFound"):
                        mark("td_security_group", "done",
                             f"Already gone: {res.get('security_group_name', '')} ({res['security_group_id']})")
                    else:
                        errors.append(str(e))
                        mark("td_security_group", "failed", str(e))
            else:
                mark("td_security_group", "done", "Skipped — not created")

        # 10 — Remove config from S3 & memory
        if _already_done("td_config"):
            pass
        else:
            mark("td_config", "running")
            # Gateway teardown: clean up the CF LB pool for this region so the
            # origin quota slot is freed. (remove_region handles this normally;
            # this guard covers direct teardown of an orphan gateway deployment.)
            if dep.get("kind") == "gateway":
                try:
                    _cf_delete_gateway_pool(region)
                    remaining = [r for r in _load_active_regions() if r != region]
                    if remaining:
                        _cf_ensure_apex_load_balancer(remaining)
                except Exception as _cf_e:
                    errors.append(f"CF pool cleanup: {_cf_e}")
            _delete_deployment_from_s3(dep_id)
            mark("td_config", "done", "Deployment config removed")

        if errors:
            dep["status"] = "teardown_partial"
            _save_deployment_to_s3(dep)  # keep partial teardown state
            _delete_pipeline(dep_id, "teardown")
        else:
            dep["status"] = "torn_down"
            _delete_pipeline(dep_id, "teardown")
            # Remove from in-memory store so it disappears from the dashboard
            DEPLOYMENTS.pop(dep_id, None)

        # Remove the (now gone) service from the gateways' routing table.
        _sync_gateway_config()

    except Exception as exc:
        dep["status"] = "teardown_partial"
        dep["_td_error"] = str(exc)
        _save_deployment_to_s3(dep)
        _delete_pipeline(dep_id, "teardown")


# ---------------------------------------------------------------------------
# Background deployment runner
# ---------------------------------------------------------------------------
def _run_deployment(dep_id: str, extra_regions=None):
    """Runs in a background thread. Updates dep['steps'] in-place.

    `extra_regions` (optional) are additional onboarded regions the service was
    created in: once the primary region is healthy, the service is onboarded into
    each of them from the same S3 JAR artifact.
    """
    dep = DEPLOYMENTS[dep_id]

    def mark(step, status, detail=""):
        dep["steps"][step] = {"status": status, "detail": detail, "label": STEP_LABELS[step]}
        dep["_version"] = dep.get("_version", 0) + 1  # bump so SSE detects changes
        _save_deployment_to_s3(dep)
        if status == "done":
            _save_pipeline(dep_id, "deploy", STEP_KEYS, dep["steps"], current_step=step, status="running")
            time.sleep(2)  # pause between steps so AWS resources settle

    try:
        dep["status"] = "deploying"
        _save_pipeline(dep_id, "deploy", STEP_KEYS, dep["steps"], status="running")

        # Shared suffix for all resources in this deployment
        deploy_uid = uuid.uuid4().hex[:6]

        # Per-deployment region (stored at deploy time)
        region = dep.get("region") or CONFIG.get("aws_region", "us-east-1")
        desired_capacity = dep.get("desired_instances", 1)

        # 0 — Create a new security group with all traffic allowed
        mark("create_sg", "running")
        ec2_sg = _ec2(region)
        sg_name = f"appmesh-sg-{dep['name'][:14]}-{deploy_uid}"
        # Get default VPC for the SG
        vpcs = ec2_sg.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])
        vpc_id_for_sg = vpcs["Vpcs"][0]["VpcId"]
        sg_resp = ec2_sg.create_security_group(
            GroupName=sg_name,
            Description=f"Security group for {dep['name']} deployment",
            VpcId=vpc_id_for_sg,
        )
        sg_id = sg_resp["GroupId"]
        ec2_sg.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[{
                "IpProtocol": "-1",
                "FromPort": -1,
                "ToPort": -1,
                "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "Allow all inbound"}],
            }],
        )
        dep["resources"]["security_group_id"] = sg_id
        dep["resources"]["security_group_name"] = sg_name
        # Validate: confirm SG exists
        sg_check = ec2_sg.describe_security_groups(GroupIds=[sg_id])
        assert sg_check["SecurityGroups"], f"SG {sg_id} not found after creation"
        mark("create_sg", "done", f"{sg_name} ({sg_id})")

        # 0b — Create IAM Role + Instance Profile for S3 access
        mark("create_iam_role", "running")
        iam = _iam(region)
        role_name = f"appmesh-role-{dep['name'][:14]}-{deploy_uid}"
        profile_name = f"appmesh-ip-{dep['name'][:16]}-{deploy_uid}"

        # Ensure service-linked roles exist (needed for ASG ↔ ELB integration)
        for svc in ("autoscaling.amazonaws.com", "elasticloadbalancing.amazonaws.com"):
            try:
                iam.create_service_linked_role(AWSServiceName=svc)
            except ClientError:
                pass  # already exists

        iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=EC2_ASSUME_ROLE_POLICY,
            Description=f"EC2 role for {dep['name']} deployment",
        )
        # Attach S3 read-only so instances can download the JAR
        iam.attach_role_policy(
            RoleName=role_name,
            PolicyArn="arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess",
        )
        # Inline policy: allow instances to tag themselves (for self-naming)
        iam.put_role_policy(
            RoleName=role_name,
            PolicyName="ec2-self-tag",
            PolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Action": ["ec2:CreateTags"],
                    "Resource": "arn:aws:ec2:*:*:instance/*",
                }],
            }),
        )
        iam.create_instance_profile(InstanceProfileName=profile_name)
        iam.add_role_to_instance_profile(
            InstanceProfileName=profile_name,
            RoleName=role_name,
        )
        dep["resources"]["iam_role_name"] = role_name
        dep["resources"]["instance_profile_name"] = profile_name

        # Validate: poll IAM until the instance profile has the role attached
        for _attempt in range(40):
            try:
                ip_check = iam.get_instance_profile(InstanceProfileName=profile_name)
                roles = ip_check["InstanceProfile"].get("Roles", [])
                if any(r["RoleName"] == role_name for r in roles):
                    break
            except ClientError:
                pass
            time.sleep(3)
        else:
            raise RuntimeError(f"Instance profile {profile_name} did not propagate after 120s")

        # Extra buffer — EC2 caches IAM; wait for cross-service propagation
        time.sleep(15)

        # Validate: get the instance profile ARN for later use
        ip_resp = iam.get_instance_profile(InstanceProfileName=profile_name)
        profile_arn = ip_resp["InstanceProfile"]["Arn"]
        dep["resources"]["instance_profile_arn"] = profile_arn
        mark("create_iam_role", "done", f"{role_name} / {profile_name}")

        # 1 — Upload to S3
        mark("upload_s3", "running")
        jar_base = dep["jar_filename"].rsplit(".jar", 1)[0]
        s3_jar_name = f"{jar_base}-{deploy_uid}.jar"
        s3_key = CONFIG.get("s3_prefix", "jars/") + s3_jar_name
        s3_uri = f"s3://{CONFIG['s3_bucket']}/{s3_key}"
        s3_client = _s3()
        s3_client.upload_file(dep["local_path"], CONFIG["s3_bucket"], s3_key)
        # Validate: confirm object exists in S3
        s3_client.head_object(Bucket=CONFIG["s3_bucket"], Key=s3_key)
        dep["resources"]["s3_uri"] = s3_uri
        dep["resources"]["s3_key"] = s3_key
        dep["resources"]["s3_jar_name"] = s3_jar_name
        mark("upload_s3", "done", s3_uri)

        # 2 — Resolve AMI
        mark("resolve_ami", "running")
        ami_id = _get_latest_al2023_ami(region)
        dep["resources"]["ami_id"] = ami_id
        mark("resolve_ami", "done", ami_id)

        # 3 — Create Launch Template
        mark("create_launch_template", "running")
        ec2 = _ec2(region)
        user_data = (USER_DATA_TEMPLATE
                     .replace("{s3_uri}", s3_uri)
                     .replace("{jar_filename}", s3_jar_name)
                     .replace("{service_name}", dep["name"])
                     .replace("{deploy_uid}", deploy_uid)
                     .replace("{aws_region}", region))
        user_data_b64 = base64.b64encode(user_data.encode()).decode()
        lt_name = f"appmesh-lt-{dep['name']}-{deploy_uid}"

        lt_resp = ec2.create_launch_template(
            LaunchTemplateName=lt_name,
            LaunchTemplateData={
                "ImageId": ami_id,
                "InstanceType": CONFIG.get("instance_type", "t3.micro"),
                "SecurityGroupIds": [sg_id],
                "UserData": user_data_b64,
                "IamInstanceProfile": {"Arn": profile_arn},
            },
        )
        lt_id = lt_resp["LaunchTemplate"]["LaunchTemplateId"]
        dep["resources"]["launch_template_id"] = lt_id
        dep["resources"]["launch_template_name"] = lt_name
        # Validate: confirm launch template exists and read it back
        lt_check = ec2.describe_launch_templates(LaunchTemplateIds=[lt_id])
        assert lt_check["LaunchTemplates"], f"Launch template {lt_id} not found"
        mark("create_launch_template", "done", f"{lt_name} ({lt_id})")

        # 4 — Discover VPC / Subnets
        mark("discover_vpc", "running")
        vpc_id, subnet_ids, azs = _get_default_vpc_and_subnets(region)
        dep["resources"]["vpc_id"] = vpc_id
        dep["resources"]["subnet_ids"] = subnet_ids
        # Validate: dry-run an instance launch to prove profile + template + networking work
        try:
            ec2.run_instances(
                LaunchTemplate={"LaunchTemplateId": lt_id, "Version": "$Latest"},
                MinCount=1, MaxCount=1,
                SubnetId=subnet_ids[0],
                DryRun=True,
            )
        except ClientError as dry_err:
            if dry_err.response["Error"]["Code"] == "DryRunOperation":
                pass  # "would have succeeded" — good
            else:
                raise RuntimeError(f"Dry-run launch validation failed: {dry_err}")
        mark("discover_vpc", "done", f"VPC {vpc_id}, {len(subnet_ids)} subnets ({', '.join(azs)}) — dry-run OK")

        # 5 — Ensure the region's shared ALB exists (created once, reused by all services)
        mark("ensure_alb", "running")
        elbv2 = _elbv2(region)
        alb_arn, alb_dns, listener_arn = _ensure_regional_alb(region, subnet_ids)
        dep["resources"]["alb_arn"] = alb_arn
        dep["resources"]["alb_dns"] = alb_dns
        dep["resources"]["alb_name"] = _regional_alb_name(region)
        dep["resources"]["listener_arn"] = listener_arn
        mark("ensure_alb", "done", f"{_regional_alb_name(region)} — {alb_dns}")

        # 6 — Create this service's Target Group
        mark("create_target_group", "running")
        tg_name = f"appmesh-tg-{dep['name'][:14]}-{deploy_uid}"
        health_path = dep.get("health_check_path", "/")
        tg_resp = elbv2.create_target_group(
            Name=tg_name,
            Protocol="HTTP",
            Port=8080,
            VpcId=vpc_id,
            TargetType="instance",
            HealthCheckProtocol="HTTP",
            HealthCheckPath=health_path,
            HealthCheckIntervalSeconds=30,
            HealthyThresholdCount=2,
            UnhealthyThresholdCount=3,
        )
        tg_arn = tg_resp["TargetGroups"][0]["TargetGroupArn"]
        dep["resources"]["target_group_arn"] = tg_arn
        dep["resources"]["target_group_name"] = tg_name
        # Set deregistration delay to 0 so instances are removed instantly
        elbv2.modify_target_group_attributes(
            TargetGroupArn=tg_arn,
            Attributes=[{"Key": "deregistration_delay.timeout_seconds", "Value": "0"}],
        )
        # Validate: confirm TG exists
        tg_check = elbv2.describe_target_groups(TargetGroupArns=[tg_arn])
        assert tg_check["TargetGroups"], f"Target group {tg_arn} not found"
        mark("create_target_group", "done", tg_name)

        # 7 — Add a host-based listener rule on the shared ALB → this service's TG
        mark("create_listener_rule", "running")
        dns_fqdn = _cf_dns_name(dep["name"], region)
        dep["resources"]["dns_fqdn"] = dns_fqdn
        rule_arn = _add_host_rule(region, listener_arn, dns_fqdn, tg_arn)
        dep["resources"]["listener_rule_arn"] = rule_arn
        mark("create_listener_rule", "done", f"Host {dns_fqdn} → {tg_name}")

        # 9 — Create ASG
        mark("create_asg", "running")
        asg_client = _autoscaling(region)
        asg_name = f"appmesh-asg-{dep['name'][:22]}-{deploy_uid}"

        asg_client.create_auto_scaling_group(
            AutoScalingGroupName=asg_name,
            LaunchTemplate={
                "LaunchTemplateId": lt_id,
                "Version": "$Latest",
            },
            MinSize=0,
            MaxSize=10,
            DesiredCapacity=desired_capacity,
            AvailabilityZones=azs,
            VPCZoneIdentifier=",".join(subnet_ids),
            TargetGroupARNs=[tg_arn],
            HealthCheckType="ELB",
            HealthCheckGracePeriod=180,
            Tags=[
                {
                    "Key": "Name",
                    "Value": asg_name,
                    "PropagateAtLaunch": False,
                    "ResourceId": asg_name,
                    "ResourceType": "auto-scaling-group",
                }
            ],
        )
        dep["resources"]["asg_name"] = asg_name
        # Validate: confirm ASG exists and has desired capacity
        asg_check = asg_client.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
        assert asg_check["AutoScalingGroups"], f"ASG {asg_name} not found"
        mark("create_asg", "done", f"{asg_name} (AZs: {', '.join(azs)})")

        # 10 — Create Cloudflare DNS CNAME record
        mark("create_dns_record", "running")
        cf_zone_id = CONFIG.get("cloudflare_zone_id", "")
        dns_fqdn = _cf_dns_name(dep["name"], region)
        dep["resources"]["dns_fqdn"] = dns_fqdn

        # Check if the record already exists — if so, fail
        existing = _cf_find_record(cf_zone_id, dns_fqdn)
        if existing:
            raise RuntimeError(
                f"DNS record {dns_fqdn} already exists in Cloudflare "
                f"(id={existing['id']}, target={existing.get('content', '?')}). "
                f"Remove it manually or choose a different deployment name."
            )

        cf_record = _cf_create_record(cf_zone_id, dns_fqdn, alb_dns)
        dep["resources"]["cloudflare_record_id"] = cf_record["id"]
        # Validate: confirm the record now exists
        verify = _cf_find_record(cf_zone_id, dns_fqdn)
        assert verify and verify["id"] == cf_record["id"], \
            f"Cloudflare record {dns_fqdn} not found after creation"
        app_url = f"http://{dns_fqdn}"
        dep["resources"]["app_url"] = app_url
        mark("create_dns_record", "done", f"{app_url} → {alb_dns}")

        # 11 — Wait for the application to answer its health endpoint.
        #       Poll ALB directly to avoid DNS-propagation delays.
        mark("wait_app_healthy", "running")
        alb_health_url = f"http://{alb_dns}{dep.get('health_check_path', '/status')}"
        healthy = False
        for _poll in range(90):  # 90 × 10s = 15 min max
            try:
                r = http_requests.get(
                    alb_health_url,
                    timeout=8,
                    allow_redirects=False,
                    headers={"Host": dns_fqdn},   # mimic real traffic
                )
                if 200 <= r.status_code < 300:
                    healthy = True
                    mark("wait_app_healthy", "done",
                         f"{app_url} is live (HTTP {r.status_code})")
                    break
                detail_msg = f"HTTP {r.status_code} — retrying ({_poll + 1}/90)…"
            except (http_requests.ConnectionError,
                    http_requests.Timeout,
                    ConnectionResetError,
                    OSError) as exc:
                detail_msg = f"{type(exc).__name__} — retrying ({_poll + 1}/90)…"
            except Exception as exc:
                detail_msg = f"{exc} — retrying ({_poll + 1}/90)…"
            mark("wait_app_healthy", "running", detail_msg)
            time.sleep(10)
        if not healthy:
            raise RuntimeError(
                f"Application at {app_url} did not become healthy after 15 minutes"
            )

        dep["status"] = "deployed"
        _save_deployment_to_s3(dep)
        _delete_pipeline(dep_id, "deploy")
        # Publish the new service into the gateways' routing table.
        _sync_gateway_config()

        # Onboard the service into any additional regions selected at create time.
        # Each is independent — a failure in one must not roll back the primary, so
        # every onboarding is isolated in its own try/except.
        for xr in (extra_regions or []):
            try:
                print(f"Onboarding {dep['name']} into additional region {xr}…")
                onboard_service_region(dep_id, xr)
            except Exception as exc:
                print(f"WARN: could not onboard {dep['name']} in {xr}: {exc}")

    except Exception as exc:
        dep["_error"] = str(exc)
        # Mark current running step as failed
        for s in STEP_KEYS:
            info = dep["steps"].get(s, {})
            if info.get("status") == "running":
                mark(s, "failed", str(exc))
                break

        # Silently rollback everything that was created
        _teardown_resources_simple(dep)

        dep["status"] = "failed"
        _save_deployment_to_s3(dep)
        _delete_deployment_from_s3(dep["id"])
        _delete_pipeline(dep_id, "deploy")
        DEPLOYMENTS.pop(dep["id"], None)


# ---------------------------------------------------------------------------
# Background JAR-update runner
# ---------------------------------------------------------------------------
def _run_update(dep_id: str):
    """Runs in a background thread. Uploads new JAR, creates a new LT version,
    starts ASG instance refresh, and waits for healthy."""
    dep = DEPLOYMENTS[dep_id]

    def mark(step, status, detail=""):
        dep["update_steps"][step] = {
            "status": status, "detail": detail,
            "label": UPDATE_STEP_LABELS[step],
        }
        dep["_upd_version"] = dep.get("_upd_version", 0) + 1
        _save_deployment_to_s3(dep)
        if status == "done":
            _save_pipeline(dep_id, "update", UPDATE_STEP_KEYS,
                           dep.get("update_steps", {}), current_step=step, status="running")
            time.sleep(2)

    try:
        dep["status"] = "updating"
        _save_pipeline(dep_id, "update", UPDATE_STEP_KEYS,
                       dep.get("update_steps", {}), status="running")
        res = dep["resources"]
        region = dep.get("region") or CONFIG.get("aws_region", "us-east-1")
        new_jar = dep["_update_jar_filename"]
        new_local = dep["_update_local_path"]

        # Snapshot the currently-active version BEFORE we overwrite it, so we can
        # push it onto the history stack once the new JAR is healthy.
        prev_version = {
            "version": dep.get("version", 1),
            "jar_filename": dep.get("jar_filename"),
            "s3_jar_name": res.get("s3_jar_name"),
            "deployed_at": dep.get("version_deployed_at") or dep.get("created_at"),
        }

        # 1 — Upload new JAR to S3
        mark("update_upload_s3", "running")
        s3_bucket = CONFIG["s3_bucket"]
        update_uid = uuid.uuid4().hex[:6]
        jar_base = new_jar.rsplit(".jar", 1)[0]
        new_s3_jar_name = f"{jar_base}-{update_uid}.jar"
        s3_key = CONFIG.get("s3_prefix", "jars/") + new_s3_jar_name
        _s3().upload_file(str(new_local), s3_bucket, s3_key)
        new_s3_uri = f"s3://{s3_bucket}/{s3_key}"
        dep["jar_filename"] = new_jar
        res["s3_uri"] = new_s3_uri
        res["s3_key"] = s3_key
        res["s3_jar_name"] = new_s3_jar_name
        # Validate
        _s3().head_object(Bucket=s3_bucket, Key=s3_key)
        mark("update_upload_s3", "done", new_s3_uri)

        # 2 — Create new Launch Template version
        mark("update_launch_template", "running")
        ec2 = _ec2(region)
        lt_id = res["launch_template_id"]

        # Build new user data with updated jar info
        user_data = (USER_DATA_TEMPLATE
                     .replace("{s3_uri}", new_s3_uri)
                     .replace("{jar_filename}", new_s3_jar_name)
                     .replace("{service_name}", dep["name"])
                     .replace("{deploy_uid}", dep["id"].rsplit("-", 1)[-1])
                     .replace("{aws_region}", region))
        user_data_b64 = base64.b64encode(user_data.encode()).decode()

        new_ver = ec2.create_launch_template_version(
            LaunchTemplateId=lt_id,
            SourceVersion="$Latest",
            LaunchTemplateData={"UserData": user_data_b64},
            VersionDescription=f"JAR update: {new_jar}",
        )
        ver_num = new_ver["LaunchTemplateVersion"]["VersionNumber"]
        # Set the new version as default
        ec2.modify_launch_template(
            LaunchTemplateId=lt_id,
            DefaultVersion=str(ver_num),
        )
        mark("update_launch_template", "done",
             f"v{ver_num} (default) — {new_jar}")

        # 3 — Start ASG instance refresh
        mark("update_asg_refresh", "running")
        asg_client = _autoscaling(region)
        asg_name = res["asg_name"]

        refresh_resp = asg_client.start_instance_refresh(
            AutoScalingGroupName=asg_name,
            Strategy="Rolling",
            Preferences={
                "MinHealthyPercentage": 100,   # keep all old instances until new ones are healthy
                "MaxHealthyPercentage": 200,   # allow double capacity during rollout
                "InstanceWarmup": 120,
            },
        )
        refresh_id = refresh_resp["InstanceRefreshId"]
        dep["resources"]["last_instance_refresh_id"] = refresh_id
        mark("update_asg_refresh", "done", f"Refresh {refresh_id}")

        # 4 — Wait for instance refresh to complete
        mark("update_wait_refresh", "running")
        for _poll in range(120):  # 120 × 15s = 30 min max
            resp = asg_client.describe_instance_refreshes(
                AutoScalingGroupName=asg_name,
                InstanceRefreshIds=[refresh_id],
            )
            refreshes = resp.get("InstanceRefreshes", [])
            if refreshes:
                r = refreshes[0]
                r_status = r["Status"]
                pct = r.get("PercentageComplete", 0)
                if r_status == "Successful":
                    mark("update_wait_refresh", "done",
                         f"Refresh completed ({pct}%)")
                    break
                elif r_status in ("Cancelled", "Failed", "RollbackSuccessful",
                                  "RollbackFailed"):
                    raise RuntimeError(
                        f"Instance refresh {r_status}: "
                        f"{r.get('StatusReason', 'unknown')}"
                    )
                detail_msg = f"{r_status} — {pct}% ({_poll + 1}/120)"
            else:
                detail_msg = f"Waiting for refresh status ({_poll + 1}/120)…"
            mark("update_wait_refresh", "running", detail_msg)
            time.sleep(15)
        else:
            raise RuntimeError("Instance refresh did not complete after 30 minutes")

        # 5 — Wait for application to be healthy (poll ALB)
        mark("update_wait_healthy", "running")
        alb_dns = res.get("alb_dns", "")
        dns_fqdn = res.get("dns_fqdn", "")
        app_url = res.get("app_url", f"http://{dns_fqdn}")
        alb_health_url = f"http://{alb_dns}{dep.get('health_check_path', '/status')}"
        healthy = False
        for _poll in range(90):
            try:
                r = http_requests.get(
                    alb_health_url, timeout=8,
                    allow_redirects=False,
                    headers={"Host": dns_fqdn},
                )
                if 200 <= r.status_code < 300:
                    healthy = True
                    mark("update_wait_healthy", "done",
                         f"{app_url} is live (HTTP {r.status_code})")
                    break
                detail_msg = f"HTTP {r.status_code} — retrying ({_poll + 1}/90)…"
            except (http_requests.ConnectionError,
                    http_requests.Timeout,
                    ConnectionResetError,
                    OSError) as exc:
                detail_msg = f"{type(exc).__name__} — retrying ({_poll + 1}/90)…"
            except Exception as exc:
                detail_msg = f"{exc} — retrying ({_poll + 1}/90)…"
            mark("update_wait_healthy", "running", detail_msg)
            time.sleep(10)
        if not healthy:
            raise RuntimeError(
                f"Application at {app_url} did not become healthy after 15 minutes"
            )

        # New JAR is live → archive the previous version (most-recent first, cap 5)
        # and bump the active version. Same deployment id/resources/region/DNS.
        prev_version["replaced_at"] = datetime.utcnow().isoformat()
        history = dep.get("history", [])
        history.insert(0, prev_version)
        dep["history"] = history[:5]
        dep["version"] = dep.get("version", 1) + 1
        dep["version_deployed_at"] = datetime.utcnow().isoformat()

        dep["status"] = "deployed"
        _save_deployment_to_s3(dep)
        _delete_pipeline(dep_id, "update")

    except Exception as exc:
        dep["_update_error"] = str(exc)
        for s in UPDATE_STEP_KEYS:
            info = dep["update_steps"].get(s, {})
            if info.get("status") == "running":
                mark(s, "failed", str(exc))
                break
        dep["status"] = "deployed"  # revert to deployed so the user can retry
        _save_deployment_to_s3(dep)
        _delete_pipeline(dep_id, "update")


def _roll_region_to_existing_artifact(dep: dict, artifact: dict):
    """Roll one regional deployment to an already-uploaded JAR artifact."""
    res = dep["resources"]
    region = dep.get("region") or CONFIG.get("aws_region", "us-east-1")
    prev_version = {
        "version": dep.get("version", 1),
        "jar_filename": dep.get("jar_filename"),
        "s3_jar_name": res.get("s3_jar_name"),
        "deployed_at": dep.get("version_deployed_at") or dep.get("created_at"),
    }

    dep["status"] = "updating"
    _save_deployment_to_s3(dep)

    res["s3_uri"] = artifact["s3_uri"]
    res["s3_key"] = artifact["s3_key"]
    res["s3_jar_name"] = artifact["s3_jar_name"]
    dep["jar_filename"] = artifact["jar_filename"]

    user_data = (USER_DATA_TEMPLATE
                 .replace("{s3_uri}", artifact["s3_uri"])
                 .replace("{jar_filename}", artifact["s3_jar_name"])
                 .replace("{service_name}", _service_key(dep))
                 .replace("{deploy_uid}", dep["id"].rsplit("-", 1)[-1])
                 .replace("{aws_region}", region))

    ec2 = _ec2(region)
    new_ver = ec2.create_launch_template_version(
        LaunchTemplateId=res["launch_template_id"],
        SourceVersion="$Latest",
        LaunchTemplateData={"UserData": base64.b64encode(user_data.encode()).decode()},
        VersionDescription=f"Service-wide JAR update: {artifact['jar_filename']}",
    )
    ver_num = new_ver["LaunchTemplateVersion"]["VersionNumber"]
    ec2.modify_launch_template(LaunchTemplateId=res["launch_template_id"],
                               DefaultVersion=str(ver_num))

    asg_client = _autoscaling(region)
    refresh = asg_client.start_instance_refresh(
        AutoScalingGroupName=res["asg_name"],
        Strategy="Rolling",
        Preferences={"MinHealthyPercentage": 100, "MaxHealthyPercentage": 200,
                     "InstanceWarmup": 120},
    )["InstanceRefreshId"]
    res["last_instance_refresh_id"] = refresh

    for _poll in range(120):
        rr = asg_client.describe_instance_refreshes(
            AutoScalingGroupName=res["asg_name"],
            InstanceRefreshIds=[refresh],
        ).get("InstanceRefreshes", [])
        if rr:
            status = rr[0]["Status"]
            if status == "Successful":
                break
            if status in ("Cancelled", "Failed", "RollbackSuccessful", "RollbackFailed"):
                raise RuntimeError(f"{dep['id']} refresh {status}: {rr[0].get('StatusReason', 'unknown')}")
        time.sleep(15)
    else:
        raise RuntimeError(f"{dep['id']} instance refresh timed out")

    elbv2 = _elbv2(region)
    for _poll in range(60):
        health = elbv2.describe_target_health(
            TargetGroupArn=res["target_group_arn"]
        ).get("TargetHealthDescriptions", [])
        if health and all(h["TargetHealth"]["State"] == "healthy" for h in health):
            break
        time.sleep(10)
    else:
        raise RuntimeError(f"{dep['id']} targets did not become healthy")

    prev_version["replaced_at"] = datetime.utcnow().isoformat()
    history = dep.get("history", [])
    history.insert(0, prev_version)
    dep["history"] = history[:5]
    dep["version"] = dep.get("version", 1) + 1
    dep["version_deployed_at"] = datetime.utcnow().isoformat()
    dep["status"] = "deployed"
    _save_deployment_to_s3(dep)


def _run_service_update(dep_id: str):
    """Update the clicked regional copy, then roll every sibling region."""
    dep = DEPLOYMENTS[dep_id]
    if not _is_user_service(dep):
        _run_update(dep_id)
        return

    service_name = _service_key(dep)
    owner = dep.get("owner") or "root"
    siblings = [d for d in _service_deployments(service_name, owner)
                if d["id"] != dep_id and d.get("status") == "deployed"]

    _run_update(dep_id)
    dep = DEPLOYMENTS.get(dep_id, dep)
    if dep.get("_update_error"):
        return

    artifact = {
        "jar_filename": dep.get("jar_filename"),
        "s3_uri": dep["resources"].get("s3_uri"),
        "s3_key": dep["resources"].get("s3_key"),
        "s3_jar_name": dep["resources"].get("s3_jar_name"),
    }
    if not all(artifact.values()):
        dep["_update_error"] = "Primary update succeeded but artifact metadata was incomplete"
        _save_deployment_to_s3(dep)
        return

    if siblings:
        dep["status"] = "updating"
        dep.setdefault("update_steps", {})
        dep["update_steps"]["update_wait_healthy"] = {
            "status": "running",
            "label": UPDATE_STEP_LABELS["update_wait_healthy"],
            "detail": f"Rolling {len(siblings)} sibling region(s)…",
        }
        dep["_upd_version"] = dep.get("_upd_version", 0) + 1
        _save_deployment_to_s3(dep)

    errors = []
    for sibling in siblings:
        try:
            _roll_region_to_existing_artifact(sibling, artifact)
        except Exception as exc:
            sibling["status"] = "deployed"
            sibling["_update_error"] = str(exc)
            _save_deployment_to_s3(sibling)
            errors.append(f"{sibling.get('region')}: {exc}")

    if siblings:
        dep["status"] = "deployed"
        dep["update_steps"]["update_wait_healthy"] = {
            "status": "failed" if errors else "done",
            "label": UPDATE_STEP_LABELS["update_wait_healthy"],
            "detail": "; ".join(errors) if errors else f"Rolled {len(siblings)} sibling region(s)",
        }
        dep["_update_error"] = "; ".join(errors)
        dep["_upd_version"] = dep.get("_upd_version", 0) + 1
        _save_deployment_to_s3(dep)


# ---------------------------------------------------------------------------
# SSE helper
# ---------------------------------------------------------------------------
def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


# ---------------------------------------------------------------------------
# AWS / Cloudflare full scan
# ---------------------------------------------------------------------------
SCAN_RESULT: dict = {}          # Shared mutable state for scan progress
SCAN_LOCK = threading.Lock()

def _run_scan():
    """Scan all appmesh-* resources across regions + Cloudflare + S3 configs.
    Populates SCAN_RESULT in-place so the SSE endpoint can stream progress."""
    global SCAN_RESULT
    SCAN_RESULT = {"status": "running", "_version": 0, "steps": [], "groups": {}, "orphan_s3_files": []}

    def _emit(step, detail=""):
        SCAN_RESULT["steps"].append({"step": step, "detail": detail})
        SCAN_RESULT["_version"] = SCAN_RESULT.get("_version", 0) + 1

    groups: dict[str, dict] = {}  # deploy_uid -> { region, resources }
    # Shared regional infrastructure (one ALB per region, host-routed to per-service
    # target groups). NOT per-deployment, so it must not become its own group.
    region_albs: dict[str, dict] = {}  # region -> {alb_*, listener_arn, rules:{tg_arn:host}}
    profile_to_role: dict[str, str] = {}  # instance_profile_arn -> role name (for display)

    def _ensure_group(uid, region=None):
        if uid not in groups:
            groups[uid] = {"uid": uid, "region": region, "resources": {}, "names": set()}
        if region and not groups[uid].get("region"):
            groups[uid]["region"] = region
        return groups[uid]

    try:
        # -- 1. Scan ASGs per region --
        for region in _scan_regions():
            _emit("scan_asg", f"Scanning ASGs in {region}…")
            try:
                asg_client = _autoscaling(region)
                paginator = asg_client.get_paginator("describe_auto_scaling_groups")
                for page in paginator.paginate():
                    for asg in page["AutoScalingGroups"]:
                        name = asg["AutoScalingGroupName"]
                        if not name.startswith("appmesh-asg-"):
                            continue
                        uid = name.rsplit("-", 1)[-1]
                        g = _ensure_group(uid, region)
                        g["resources"]["asg_name"] = name
                        g["resources"]["asg_desired"] = asg["DesiredCapacity"]
                        g["resources"]["asg_instances"] = len(asg.get("Instances", []))
                        if asg.get("TargetGroupARNs"):
                            g["resources"]["target_group_arn"] = asg["TargetGroupARNs"][0]
                        lt_spec = asg.get("LaunchTemplate", {})
                        if lt_spec.get("LaunchTemplateId"):
                            g["resources"]["launch_template_id"] = lt_spec["LaunchTemplateId"]
                        # Extract service name from ASG name
                        # Format: appmesh-asg-<name>-<uid>
                        parts = name[len("appmesh-asg-"):]
                        svc_name = parts[:-(len(uid) + 1)] if uid else parts
                        g["names"].add(svc_name)
            except Exception as e:
                _emit("scan_asg", f"Error scanning ASGs in {region}: {e}")

        # -- 2. Scan the shared regional ALB(s) — these are shared infra, NOT a
        #       deployment. Record them per-region and map each host-header listener
        #       rule to the target group it forwards to (so DNS/health can resolve
        #       which service a hostname belongs to).
        for region in _scan_regions():
            _emit("scan_alb", f"Scanning ALBs in {region}…")
            try:
                elbv2 = _elbv2(region)
                paginator = elbv2.get_paginator("describe_load_balancers")
                for page in paginator.paginate():
                    for lb in page["LoadBalancers"]:
                        name = lb["LoadBalancerName"]
                        if not name.startswith("appmesh-alb-"):
                            continue
                        info = region_albs.setdefault(region, {"rules": {}})
                        info["alb_arn"] = lb["LoadBalancerArn"]
                        info["alb_dns"] = lb["DNSName"]
                        info["alb_name"] = name
                        info["alb_state"] = lb["State"]["Code"]
                        try:
                            lr = elbv2.describe_listeners(LoadBalancerArn=lb["LoadBalancerArn"])
                            for listener in lr["Listeners"]:
                                info["listener_arn"] = listener["ListenerArn"]
                                rp = elbv2.get_paginator("describe_rules")
                                for rpage in rp.paginate(ListenerArn=listener["ListenerArn"]):
                                    for rule in rpage["Rules"]:
                                        host = None
                                        for cond in rule.get("Conditions", []):
                                            if cond.get("Field") == "host-header":
                                                vals = cond.get("HostHeaderConfig", {}).get("Values") or cond.get("Values")
                                                if vals:
                                                    host = vals[0]
                                        tg_arn = None
                                        for act in rule.get("Actions", []):
                                            if act.get("Type") == "forward" and act.get("TargetGroupArn"):
                                                tg_arn = act["TargetGroupArn"]
                                        if host and tg_arn:
                                            # A target group can back several hosts
                                            # (e.g. the gateway answers lb-ue1.* AND apex).
                                            info["rules"].setdefault(tg_arn, []).append(host)
                        except Exception:
                            pass
            except Exception as e:
                _emit("scan_alb", f"Error scanning ALBs in {region}: {e}")

        # -- 3. Scan Target Groups per region --
        for region in _scan_regions():
            _emit("scan_tg", f"Scanning Target Groups in {region}…")
            try:
                elbv2 = _elbv2(region)
                paginator = elbv2.get_paginator("describe_target_groups")
                for page in paginator.paginate():
                    for tg in page["TargetGroups"]:
                        name = tg["TargetGroupName"]
                        if not name.startswith("appmesh-tg-"):
                            continue
                        uid = name.rsplit("-", 1)[-1]
                        g = _ensure_group(uid, region)
                        if "target_group_arn" not in g["resources"]:
                            g["resources"]["target_group_arn"] = tg["TargetGroupArn"]
                        g["resources"]["target_group_name"] = name
            except Exception as e:
                _emit("scan_tg", f"Error scanning TGs in {region}: {e}")

        # -- 4. Scan Launch Templates per region --
        for region in _scan_regions():
            _emit("scan_lt", f"Scanning Launch Templates in {region}…")
            try:
                ec2 = _ec2(region)
                paginator = ec2.get_paginator("describe_launch_templates")
                for page in paginator.paginate():
                    for lt in page["LaunchTemplates"]:
                        name = lt["LaunchTemplateName"]
                        if not name.startswith("appmesh-lt-"):
                            continue
                        uid = name.rsplit("-", 1)[-1]
                        g = _ensure_group(uid, region)
                        g["resources"]["launch_template_id"] = lt["LaunchTemplateId"]
                        g["resources"]["launch_template_name"] = name
                        # Get user data to extract S3 URI and jar filename
                        try:
                            ltv = ec2.describe_launch_template_versions(
                                LaunchTemplateId=lt["LaunchTemplateId"],
                                Versions=["$Latest"],
                            )
                            lt_data = ltv["LaunchTemplateVersions"][0]["LaunchTemplateData"]
                            if lt_data.get("IamInstanceProfile", {}).get("Arn"):
                                g["resources"]["instance_profile_arn"] = lt_data["IamInstanceProfile"]["Arn"]
                                # Extract profile name from ARN
                                ip_name = lt_data["IamInstanceProfile"]["Arn"].rsplit("/", 1)[-1]
                                g["resources"]["instance_profile_name"] = ip_name
                            if lt_data.get("SecurityGroupIds"):
                                g["resources"]["security_group_id"] = lt_data["SecurityGroupIds"][0]
                            if lt_data.get("UserData"):
                                ud = base64.b64decode(lt_data["UserData"]).decode("utf-8", errors="replace")
                                # Extract S3 URI: aws s3 cp "s3://bucket/jars/xxx.jar"
                                s3_match = re.search(r's3://[^\s"]+\.jar', ud)
                                if s3_match:
                                    g["resources"]["s3_uri"] = s3_match.group(0)
                                    s3_key_match = re.search(r's3://[^/]+/(.+\.jar)', s3_match.group(0))
                                    if s3_key_match:
                                        g["resources"]["s3_key"] = s3_key_match.group(1)
                                        g["resources"]["s3_jar_name"] = s3_key_match.group(1).rsplit("/", 1)[-1]
                                jar_match = re.search(r'/app/([^\s"]+\.jar)', ud)
                                if jar_match:
                                    g["resources"]["_jar_local_name"] = jar_match.group(1)
                        except Exception:
                            pass
                        # Extract service name from LT name
                        parts = name[len("appmesh-lt-"):]
                        svc_name = parts[:-(len(uid) + 1)] if uid else parts
                        g["names"].add(svc_name)
            except Exception as e:
                _emit("scan_lt", f"Error scanning LTs in {region}: {e}")

        # -- 5. Scan Security Groups per region --
        for region in _scan_regions():
            _emit("scan_sg", f"Scanning Security Groups in {region}…")
            try:
                ec2 = _ec2(region)
                paginator = ec2.get_paginator("describe_security_groups")
                for page in paginator.paginate(Filters=[{"Name": "group-name", "Values": ["appmesh-sg-*"]}]):
                    for sg in page["SecurityGroups"]:
                        name = sg["GroupName"]
                        uid = name.rsplit("-", 1)[-1]
                        g = _ensure_group(uid, region)
                        g["resources"]["security_group_id"] = sg["GroupId"]
                        g["resources"]["security_group_name"] = name
            except Exception as e:
                _emit("scan_sg", f"Error scanning SGs in {region}: {e}")

        # -- 6. Scan IAM instance profiles (global, SHARED across stacks). Roles are
        #       no longer per-deployment (appmesh-gateway-role / appmesh-console-role),
        #       so map each shared instance-profile ARN -> its role for display; groups
        #       reference a profile via the ARN captured from their launch template.
        _emit("scan_iam", "Scanning IAM Roles & Instance Profiles…")
        try:
            iam = _iam()
            paginator = iam.get_paginator("list_instance_profiles")
            for page in paginator.paginate():
                for ip in page["InstanceProfiles"]:
                    name = ip["InstanceProfileName"]
                    if not name.startswith("appmesh-"):
                        continue
                    roles = ip.get("Roles", [])
                    profile_to_role[ip["Arn"]] = roles[0]["RoleName"] if roles else name
        except Exception as e:
            _emit("scan_iam", f"Error scanning IAM: {e}")

        # -- 6b. Attach SHARED infra to each per-deployment group: the regional ALB,
        #        its listener, the IAM role behind the instance profile, and the
        #        hostname the ALB host-rule routes to this group's target group.
        all_alb_dns = {info.get("alb_dns", "").lower() for info in region_albs.values() if info.get("alb_dns")}
        host_to_uid: dict[str, str] = {}
        for uid, g in groups.items():
            region = g.get("region")
            info = region_albs.get(region, {})
            for k in ("alb_arn", "alb_dns", "alb_name", "alb_state", "listener_arn"):
                if info.get(k):
                    g["resources"][k] = info[k]
            tg_arn = g["resources"].get("target_group_arn")
            hosts = info.get("rules", {}).get(tg_arn, []) if tg_arn else []
            for h in hosts:
                host_to_uid[h.lower()] = uid
            if hosts:
                # Prefer the most specific hostname (lb-ue1.appmesh.org over apex appmesh.org).
                g["resources"]["dns_fqdn_expected"] = sorted(hosts, key=lambda h: h.count("."), reverse=True)[0]
            ip_arn = g["resources"].get("instance_profile_arn")
            if ip_arn and ip_arn in profile_to_role:
                g["resources"]["iam_role_name"] = profile_to_role[ip_arn]

        # -- 7. Scan Cloudflare DNS records. A record is matched to a deployment when
        #       its hostname equals the host-header the ALB rule forwards to that
        #       deployment's target group (all records share the one ALB DNS, so we
        #       can't match on the CNAME target alone). Records that point at the
        #       shared ALB but match no live host-rule are flagged as orphan DNS.
        _emit("scan_dns", "Scanning Cloudflare DNS records…")
        try:
            cf_zone_id = CONFIG.get("cloudflare_zone_id", "")
            cf_domain = CONFIG.get("cloudflare_domain", "appmesh.org")
            if cf_zone_id:
                url = f"{CF_API_BASE}/zones/{cf_zone_id}/dns_records"
                params = {"type": "CNAME", "per_page": 100}
                resp = http_requests.get(url, headers=_cf_headers(), params=params)
                resp.raise_for_status()
                records = resp.json().get("result", [])
                for rec in records:
                    fqdn = rec["name"]
                    if not (fqdn == cf_domain or fqdn.endswith(f".{cf_domain}")):
                        continue
                    target = rec.get("content", "").lower()
                    uid = host_to_uid.get(fqdn.lower())
                    if uid:
                        g = groups[uid]
                        g["resources"]["dns_fqdn"] = fqdn
                        g["resources"]["cloudflare_record_id"] = rec["id"]
                        g["resources"]["app_url"] = f"http://{fqdn}"
                    elif target in all_alb_dns:
                        # Points at our shared ALB but no host-rule routes it → orphan
                        orphan_uid = f"dns-orphan-{rec['id'][:8]}"
                        og = _ensure_group(orphan_uid)
                        og["resources"]["dns_fqdn"] = fqdn
                        og["resources"]["cloudflare_record_id"] = rec["id"]
                        og["resources"]["_orphan_dns"] = True
                        og["names"].add(fqdn)
                    # else: a record unrelated to AppMesh ALBs — ignore.
        except Exception as e:
            _emit("scan_dns", f"Error scanning Cloudflare: {e}")

        # -- 8. Scan tracked deployments (DynamoDB) --
        _emit("scan_s3_config", "Scanning tracked deployments…")
        s3_configs: dict[str, dict] = {}  # dep_id -> config
        try:
            for item in _deployments_table().scan().get("Items", []):
                try:
                    cfg = json.loads(item["data"])
                    s3_configs[cfg.get("id", item.get("dep_id"))] = cfg
                except Exception:
                    continue
        except Exception as e:
            _emit("scan_s3_config", f"Error scanning tracked deployments: {e}")

        # -- 9. Match S3 configs to discovered groups --
        _emit("matching", "Matching S3 configs to AWS resources…")
        matched_s3_ids = set()
        for dep_id, cfg in s3_configs.items():
            # Match by a PER-DEPLOYMENT resource id (the ALB/role are shared, so they
            # can't disambiguate which deployment a config belongs to).
            cfg_res = cfg.get("resources", {})
            found_uid = None
            for uid, g in groups.items():
                g_res = g["resources"]
                if (cfg_res.get("asg_name") and g_res.get("asg_name") == cfg_res["asg_name"]) or \
                   (cfg_res.get("launch_template_id") and g_res.get("launch_template_id") == cfg_res["launch_template_id"]) or \
                   (cfg_res.get("target_group_arn") and g_res.get("target_group_arn") == cfg_res["target_group_arn"]) or \
                   (cfg_res.get("security_group_id") and g_res.get("security_group_id") == cfg_res["security_group_id"]):
                    found_uid = uid
                    break
            if found_uid:
                groups[found_uid]["s3_config"] = cfg
                groups[found_uid]["s3_dep_id"] = dep_id
                matched_s3_ids.add(dep_id)
            else:
                # S3 config has no matching AWS resources = orphan
                pass

        # Find orphaned S3 files (configs with no matching AWS resources)
        orphan_s3 = []
        for dep_id, cfg in s3_configs.items():
            if dep_id not in matched_s3_ids:
                orphan_s3.append({
                    "dep_id": dep_id,
                    "name": cfg.get("name", "unknown"),
                    "status": cfg.get("status", "unknown"),
                    "region": cfg.get("region", "unknown"),
                    "s3_key": f"{S3_CONF_PREFIX}{dep_id}.json",
                })

        # -- 10. Determine health status for each group --
        #   A deployment is the per-stack set {SG, launch template, target group, ASG}
        #   (the ALB, listener and IAM role are SHARED and attached for display only).
        #   healthy    = all core resources present AND tracked in DynamoDB
        #   broken     = AWS resources exist but are untracked, or a core piece is missing
        #   orphan_dns = a Cloudflare record with no live host-rule behind it
        _emit("analyzing", "Analyzing deployment health…")
        CORE_RESOURCES = ["security_group_id", "launch_template_id", "target_group_arn", "asg_name"]
        DISPLAY_RESOURCES = CORE_RESOURCES + [
            "iam_role_name", "instance_profile_name", "alb_arn", "listener_arn", "dns_fqdn",
        ]
        final_groups = {}
        for uid, g in groups.items():
            res = g["resources"]
            cfg = g.get("s3_config") or {}
            tracked = bool(cfg)
            present = [k for k in DISPLAY_RESOURCES if res.get(k)]
            missing = [k for k in DISPLAY_RESOURCES if not res.get(k)]
            core_present = all(res.get(k) for k in CORE_RESOURCES)

            if res.get("_orphan_dns"):
                health = "orphan_dns"
            elif not any(res.get(k) for k in CORE_RESOURCES):
                continue  # nothing real here, skip
            elif tracked and core_present:
                health = "healthy"
            else:
                health = "broken"

            # Prefer the tracked record's name/kind; fall back to the name parsed
            # from the resource names (appmesh-asg-<name>-<uid>).
            names = list(g.get("names", set()))
            best_name = cfg.get("name") or (names[0] if names else f"unknown-{uid}")
            kind = cfg.get("kind")
            if not kind:
                if best_name == "console":
                    kind = "console"
                elif best_name.startswith("gateway"):
                    kind = "gateway"
                elif best_name == "landing":
                    kind = "landing"
                elif not res.get("_orphan_dns"):
                    kind = "service"

            final_groups[uid] = {
                "uid": uid,
                "name": best_name,
                "kind": kind,
                "region": g.get("region", "unknown"),
                "health": health,
                "tracked": tracked,
                "resources": {k: v for k, v in res.items() if not k.startswith("_")},
                "present": present,
                "missing": missing,
                "s3_dep_id": g.get("s3_dep_id"),
                "has_s3_config": tracked,
            }

        SCAN_RESULT["groups"] = final_groups
        SCAN_RESULT["orphan_s3_files"] = orphan_s3
        SCAN_RESULT["finished_at"] = time.time()
        SCAN_RESULT["status"] = "done"
        _emit("done", f"Scan complete: {len(final_groups)} deployment(s) found, {len(orphan_s3)} orphaned S3 config(s)")

    except Exception as e:
        SCAN_RESULT["finished_at"] = time.time()
        SCAN_RESULT["status"] = "error"
        _emit("error", str(e))


# ============================================================================
# Infrastructure provisioning
#   Shared by the console UI and the thin initialize_infrastructure.py CLI.
#   - SUPPORTED_REGIONS is the menu the console offers for onboarding.
#   - The ACTIVE (onboarded) region set is DERIVED from the deployment registry
#     (DynamoDB): a region is active iff it has a deployed gateway. There is no
#     separate state file, so the active set can never drift from reality.
#   - One shared ALB per region; gateways (JAR) per region; console in us-east-1.
# ============================================================================
# AWS regions enabled by default (no per-account opt-in required). To offer an
# opt-in region (e.g. eu-central-2, me-central-1, af-south-1), enable it on the
# account first, then add it here.
#   region -> (abbrev, friendly label)
# The abbrev keeps AWS resource + host names short (gateway-<abbrev>,
# lb-<abbrev>.appmesh.org); the label is shown in the console's region picker.
REGIONS = {
    "us-east-1":      ("ue1",  "US East (N. Virginia)"),
    "us-east-2":      ("ue2",  "US East (Ohio)"),
    "us-west-1":      ("uw1",  "US West (N. California)"),
    "us-west-2":      ("uw2",  "US West (Oregon)"),
    "ca-central-1":   ("cc1",  "Canada (Central)"),
    "sa-east-1":      ("se1",  "South America (São Paulo)"),
    "eu-west-1":      ("ew1",  "Europe (Ireland)"),
    "eu-west-2":      ("ew2",  "Europe (London)"),
    "eu-west-3":      ("ew3",  "Europe (Paris)"),
    "eu-central-1":   ("ec1",  "Europe (Frankfurt)"),
    "eu-north-1":     ("en1",  "Europe (Stockholm)"),
    "ap-south-1":     ("as1",  "Asia Pacific (Mumbai)"),
    "ap-southeast-1": ("ase1", "Asia Pacific (Singapore)"),
    "ap-southeast-2": ("ase2", "Asia Pacific (Sydney)"),
    "ap-northeast-1": ("ane1", "Asia Pacific (Tokyo)"),
    "ap-northeast-2": ("ane2", "Asia Pacific (Seoul)"),
    "ap-northeast-3": ("ane3", "Asia Pacific (Osaka)"),
}
REGION_ABBREV = {_r: _a for _r, (_a, _l) in REGIONS.items()}
REGION_LABELS = {_r: _l for _r, (_a, _l) in REGIONS.items()}
SUPPORTED_REGIONS = list(REGIONS.keys())
CONSOLE_REGION = "us-east-1"
MAX_ONBOARDED_REGIONS = int(CONFIG.get("max_onboarded_regions", "2"))

# Cloudflare Load Balancing is account-scoped for monitors/pools and zone-scoped
# for the appmesh.org load balancer itself.
CF_LB_NAME = CONFIG.get("cloudflare_lb_name", CONFIG.get("cloudflare_domain", "appmesh.org"))
CF_LB_MONITOR_NAME = CONFIG.get("cloudflare_lb_monitor_name", "appmesh-gateway-status")
CF_LB_STEERING_POLICY = CONFIG.get("cloudflare_lb_steering_policy", "proximity")
CF_LB_ORIGIN_PREFIX = CONFIG.get("cloudflare_lb_pool_prefix", "appmesh")
CF_LB_CHECK_REGIONS = [
    r.strip() for r in CONFIG.get("cloudflare_lb_check_regions", "ENAM").split(",")
    if r.strip()
]
CF_REGION_COORDS = {
    "us-east-1": (39.0438, -77.4874),
    "us-east-2": (40.4173, -82.9071),
    "us-west-1": (37.3382, -121.8863),
    "us-west-2": (45.5152, -122.6784),
    "ca-central-1": (45.5019, -73.5674),
    "sa-east-1": (-23.5558, -46.6396),
    "eu-west-1": (53.3498, -6.2603),
    "eu-west-2": (51.5072, -0.1276),
    "eu-west-3": (48.8566, 2.3522),
    "eu-central-1": (50.1109, 8.6821),
    "eu-north-1": (59.3293, 18.0686),
    "ap-south-1": (19.0760, 72.8777),
    "ap-southeast-1": (1.3521, 103.8198),
    "ap-southeast-2": (-33.8688, 151.2093),
    "ap-northeast-1": (35.6762, 139.6503),
    "ap-northeast-2": (37.5665, 126.9780),
    "ap-northeast-3": (34.6937, 135.5023),
}

APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent
BUNDLED_JAR_DIR = APP_DIR / "bundled_jars"
GATEWAY_JAR_NAME = "gateway-lb-0.0.1-SNAPSHOT.jar"
LANDING_JAR_NAME = "landing-service.jar"
GATEWAY_JAR_PATH = str(REPO_ROOT / "lb" / "target" / GATEWAY_JAR_NAME)
LANDING_JAR_PATH = str(REPO_ROOT / "apps" / "landing" / "target" / LANDING_JAR_NAME)
GATEWAY_ROLE = "appmesh-gateway-role"
GATEWAY_PROFILE = "appmesh-gateway-ip"
CONSOLE_ROLE = "appmesh-console-role"
CONSOLE_PROFILE = "appmesh-console-ip"

CONSOLE_USER_DATA_TEMPLATE = r"""#!/bin/bash
exec > /var/log/user-data.log 2>&1
set -x
IID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null || echo unknown)
BUCKET="{s3_bucket}"

# Self-diagnostics: push the boot log + service state to S3 so failures are
# visible without SSM/SSH. Runs both on early exit (trap) and after the wait.
dump_diag() {
  {
    echo "=== dump at $(date) (rc=$?) ==="
    echo "=== python/pip ==="; python3 --version; /app/venv/bin/python --version 2>&1
    echo "=== ls /app/venv/bin ==="; ls -la /app/venv/bin 2>&1 | head -n 30
    echo "=== pip freeze ==="; /app/venv/bin/pip freeze 2>&1 | head -n 50
    echo "=== ls /app ==="; ls -la /app 2>&1
    echo "=== systemctl status ==="; systemctl status appmesh-console --no-pager 2>&1 | head -n 40
    echo "=== journal ==="; journalctl -u appmesh-console --no-pager 2>&1 | tail -n 150
    echo "=== ss :8080 ==="; ss -ltnp 2>&1 | grep -E '8080|State' || true
    echo "=== free -m ==="; free -m
  } >> /var/log/user-data.log 2>&1
  aws s3 cp /var/log/user-data.log "s3://${BUCKET}/diag/console-${IID}.log" 2>/dev/null || true
}
trap dump_diag EXIT

dnf -y install python3 python3-pip unzip

if ! command -v aws &>/dev/null; then
  curl -sL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
  unzip -qo /tmp/awscliv2.zip -d /tmp && /tmp/aws/install
fi

mkdir -p /app
for i in $(seq 1 12); do
  aws s3 cp "{s3_uri}" /app/console.zip && break
  echo "S3 download attempt $i failed, retrying..."; sleep 10
done
unzip -qo /app/console.zip -d /app

# Robust venv: fall back to manual pip bootstrap if ensurepip is unavailable.
python3 -m venv /app/venv || python3 -m venv --without-pip /app/venv
if [ ! -x /app/venv/bin/pip ]; then
  curl -sL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
  /app/venv/bin/python /tmp/get-pip.py
fi
/app/venv/bin/pip install --upgrade pip
/app/venv/bin/pip install -r /app/requirements.txt

cat >/etc/systemd/system/appmesh-console.service <<'EOF'
[Unit]
Description=AppMesh Console
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/app
Environment=APPMESH_BOOTSTRAP=1
# One worker keeps in-memory operation state coherent. The threaded worker lets
# long-lived SSE streams coexist with /status health checks and normal UI traffic.
ExecStart=/app/venv/bin/gunicorn -w 1 -k gthread --threads 16 -b 0.0.0.0:8080 --timeout 900 --graceful-timeout 60 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable appmesh-console
systemctl restart appmesh-console

# Let the service boot (or crash-loop) a few times, then snapshot state to S3.
sleep 45
dump_diag
"""


# ── Active-region registry (derived from the deployment table) ──────
def _load_active_regions() -> list:
    """Onboarded regions = regions that currently have a deployed gateway.
    Derived from the in-memory deployment registry (a mirror of the DynamoDB
    deployments table), so it can never drift the way a separate state file could.
    """
    return sorted({
        d.get("region") for d in DEPLOYMENTS.values()
        if d.get("kind") == "gateway"
        and d.get("status") not in ("torn_down", "teardown_partial")
        and d.get("region")
    })


def _scan_regions() -> list:
    """Regions the AWS scan inspects: onboarded regions plus the console region."""
    return sorted(set(_load_active_regions()) | {CONSOLE_REGION})


# ── IAM roles (global, created once, shared across stacks) ──────────
def _ensure_role(role_name: str, profile_name: str, managed_arns: list) -> str:
    iam = _iam("us-east-1")
    try:
        iam.get_role(RoleName=role_name)
    except ClientError:
        iam.create_role(RoleName=role_name,
                        AssumeRolePolicyDocument=EC2_ASSUME_ROLE_POLICY,
                        Description=f"AppMesh {role_name}")
    for arn in managed_arns:
        try:
            iam.attach_role_policy(RoleName=role_name, PolicyArn=arn)
        except ClientError:
            pass
    try:
        iam.create_instance_profile(InstanceProfileName=profile_name)
    except ClientError:
        pass
    try:
        iam.add_role_to_instance_profile(InstanceProfileName=profile_name, RoleName=role_name)
    except ClientError:
        pass
    try:
        iam.get_waiter("instance_profile_exists").wait(InstanceProfileName=profile_name)
    except Exception:
        pass
    time.sleep(8)  # IAM propagation
    return iam.get_instance_profile(InstanceProfileName=profile_name)["InstanceProfile"]["Arn"]


def ensure_gateway_role() -> str:
    return _ensure_role(GATEWAY_ROLE, GATEWAY_PROFILE,
                        ["arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess"])


def ensure_console_role() -> str:
    # Broad role: the console creates/deletes EC2/ELB/ASG/IAM/S3/DynamoDB resources.
    return _ensure_role(CONSOLE_ROLE, CONSOLE_PROFILE, [
        "arn:aws:iam::aws:policy/AmazonEC2FullAccess",
        "arn:aws:iam::aws:policy/ElasticLoadBalancingFullAccess",
        "arn:aws:iam::aws:policy/AutoScalingFullAccess",
        "arn:aws:iam::aws:policy/AmazonS3FullAccess",
        "arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess",
        "arn:aws:iam::aws:policy/IAMFullAccess",
    ])


def _ensure_service_linked_roles():
    """Ensure the ASG + ELB service-linked roles exist (needed for ASG↔ELB integration)."""
    iam = _iam("us-east-1")
    for svc in ("autoscaling.amazonaws.com", "elasticloadbalancing.amazonaws.com"):
        try:
            iam.create_service_linked_role(AWSServiceName=svc)
        except ClientError:
            pass  # already exists


# ── Generic stack provisioning (SG → LT → shared ALB → TG → rule → ASG) ──
def provision_stack(name, region, profile_arn, user_data, health_check_path,
                    host_fqdn, desired=1, instance_type=None, grace=300) -> dict:
    ec2 = _ec2(region)
    elbv2 = _elbv2(region)
    asg = _autoscaling(region)
    uid = uuid.uuid4().hex[:8]
    res: dict = {}

    _ensure_service_linked_roles()

    vpc_id, subnet_ids, azs = _get_default_vpc_and_subnets(region)

    sg_id = ec2.create_security_group(
        GroupName=f"appmesh-sg-{name}-{uid}", Description=f"AppMesh {name}", VpcId=vpc_id,
    )["GroupId"]
    ec2.authorize_security_group_ingress(
        GroupId=sg_id,
        IpPermissions=[{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}],
    )
    res["security_group_id"] = sg_id

    ami = _get_latest_al2023_ami(region)
    lt_name = f"appmesh-lt-{name}-{uid}"
    lt = ec2.create_launch_template(LaunchTemplateName=lt_name, LaunchTemplateData={
        "ImageId": ami,
        "InstanceType": instance_type or CONFIG.get("instance_type", "t3.micro"),
        "SecurityGroupIds": [sg_id],
        "UserData": base64.b64encode(user_data.encode()).decode(),
        "IamInstanceProfile": {"Arn": profile_arn},
    })["LaunchTemplate"]
    res["launch_template_id"] = lt["LaunchTemplateId"]
    res["launch_template_name"] = lt_name

    alb_arn, alb_dns, listener_arn = _ensure_regional_alb(region, subnet_ids)
    res["alb_arn"] = alb_arn
    res["alb_dns"] = alb_dns
    res["alb_name"] = _regional_alb_name(region)
    res["listener_arn"] = listener_arn

    # ELB target-group names are capped at 32 chars: "appmesh-tg-"(11) + name + "-"(1) + uid(8).
    tg_name = f"appmesh-tg-{name[:12]}-{uid}"
    tg_arn = elbv2.create_target_group(
        Name=tg_name, Protocol="HTTP", Port=8080, VpcId=vpc_id, TargetType="instance",
        HealthCheckProtocol="HTTP", HealthCheckPath=health_check_path,
        HealthCheckIntervalSeconds=30, HealthyThresholdCount=2, UnhealthyThresholdCount=3,
    )["TargetGroups"][0]["TargetGroupArn"]
    elbv2.modify_target_group_attributes(
        TargetGroupArn=tg_arn,
        Attributes=[{"Key": "deregistration_delay.timeout_seconds", "Value": "0"}],
    )
    res["target_group_arn"] = tg_arn
    res["target_group_name"] = tg_name
    res["health_check_path"] = health_check_path

    res["listener_rule_arn"] = _add_host_rule(region, listener_arn, host_fqdn, tg_arn)

    asg_name = f"appmesh-asg-{name[:22]}-{uid}"
    asg.create_auto_scaling_group(
        AutoScalingGroupName=asg_name,
        LaunchTemplate={"LaunchTemplateId": res["launch_template_id"], "Version": "$Latest"},
        MinSize=0, MaxSize=10, DesiredCapacity=desired,
        AvailabilityZones=azs, VPCZoneIdentifier=",".join(subnet_ids),
        TargetGroupARNs=[tg_arn], HealthCheckType="ELB", HealthCheckGracePeriod=grace,
        Tags=[{"Key": "Name", "Value": asg_name, "PropagateAtLaunch": True}],
    )
    res["asg_name"] = asg_name
    res["asg_desired"] = desired
    res["dns_fqdn"] = host_fqdn
    return res


def _ensure_cf_record(host: str, target: str, res: dict):
    zone = CONFIG.get("cloudflare_zone_id", "")
    existing = _cf_find_record(zone, host)
    if existing:
        # A recreated ALB has a new DNS name — repoint the stale record at it.
        if existing.get("content") != target:
            _cf_update_record(zone, existing["id"], host, target)
        res["cloudflare_record_id"] = existing["id"]
        return
    res["cloudflare_record_id"] = _cf_create_record(zone, host, target)["id"]


def _cf_api(method: str, path: str, *, ok_missing: bool = False, **kwargs):
    """Cloudflare API wrapper that returns result and raises readable failures."""
    if not _cloudflare_enabled():
        return None
    url = path if path.startswith("http") else f"{CF_API_BASE}{path}"
    resp = None
    for attempt in range(4):
        resp = http_requests.request(method, url, headers=_cf_headers(), timeout=30, **kwargs)
        if resp.status_code not in (429, 500, 502, 503, 504):
            break
        if attempt < 3:
            time.sleep(2 * (attempt + 1))
    if ok_missing and resp.status_code == 404:
        return None
    try:
        body = resp.json()
    except Exception:
        body = {"raw": resp.text}
    if resp.status_code >= 400:
        detail = body.get("errors") or body
        raise RuntimeError(f"Cloudflare {method} {url} -> HTTP {resp.status_code}: {detail}")
    if not body.get("success"):
        raise RuntimeError(f"Cloudflare API error: {body.get('errors', body)}")
    return body.get("result")


def _cf_account_id() -> str:
    """Account id for account-scoped LB monitor/pool APIs."""
    account_id = CONFIG.get("cloudflare_account_id", "").strip()
    if account_id:
        return account_id
    zone_id = CONFIG.get("cloudflare_zone_id", "")
    result = _cf_api("GET", f"/zones/{zone_id}")
    account_id = ((result or {}).get("account") or {}).get("id", "")
    if not account_id:
        raise RuntimeError("Set cloudflare_account_id in config.properties for Cloudflare Load Balancing")
    return account_id


def _cf_find_named(items: list[dict], name: str) -> dict | None:
    for item in items or []:
        if item.get("name") == name:
            return item
    return None


def _cf_list_monitors(account_id: str) -> list[dict]:
    return _cf_api("GET", f"/accounts/{account_id}/load_balancers/monitors") or []


def _cf_list_pools(account_id: str) -> list[dict]:
    return _cf_api("GET", f"/accounts/{account_id}/load_balancers/pools") or []


def _cf_list_load_balancers(zone_id: str) -> list[dict]:
    return _cf_api("GET", f"/zones/{zone_id}/load_balancers") or []


def _cf_gateway_monitor_payload() -> dict:
    return {
        "name": CF_LB_MONITOR_NAME,
        "description": "AppMesh regional gateway /status monitor",
        "type": "http",
        "method": "GET",
        "path": "/status",
        "expected_codes": "200",
        "expected_body": "UP",
        "follow_redirects": True,
        "allow_insecure": False,
        "header": {"Host": [CF_LB_NAME]},
        "interval": int(CONFIG.get("cloudflare_lb_monitor_interval", "60")),
        "timeout": int(CONFIG.get("cloudflare_lb_monitor_timeout", "5")),
        "consecutive_down": int(CONFIG.get("cloudflare_lb_consecutive_down", "2")),
        "consecutive_up": int(CONFIG.get("cloudflare_lb_consecutive_up", "2")),
    }


def _cf_ensure_gateway_monitor(account_id: str, log=print) -> str:
    payload = _cf_gateway_monitor_payload()
    existing = None
    for monitor in _cf_list_monitors(account_id):
        if monitor.get("description") == payload["description"]:
            existing = monitor
            break
        if (monitor.get("path") == payload["path"]
                and monitor.get("expected_body") == payload["expected_body"]
                and monitor.get("type") == payload["type"]):
            existing = monitor
            break
    if existing:
        _cf_api("PUT", f"/accounts/{account_id}/load_balancers/monitors/{existing['id']}",
                json=payload)
        return existing["id"]
    log(f"Creating Cloudflare monitor {CF_LB_MONITOR_NAME} (/status)…")
    return _cf_api("POST", f"/accounts/{account_id}/load_balancers/monitors",
                   json=payload)["id"]


def _cf_pool_name(region: str) -> str:
    return f"{CF_LB_ORIGIN_PREFIX}-{REGION_ABBREV[region]}"


def _cf_gateway_origin_host(region: str) -> str:
    return f"lb-{REGION_ABBREV[region]}.{CONFIG.get('cloudflare_domain', 'appmesh.org')}"


def _active_gateway_deployment(region: str) -> dict | None:
    gateways = [
        d for d in DEPLOYMENTS.values()
        if d.get("kind") == "gateway"
        and d.get("region") == region
        and d.get("status") not in ("torn_down", "teardown_partial")
    ]
    if not gateways:
        return None
    return sorted(gateways, key=lambda d: d.get("created_at", ""))[-1]


def _gateway_alb_dns(region: str) -> str:
    dep = _active_gateway_deployment(region)
    res = (dep or {}).get("resources", {})
    target = res.get("alb_dns", "")
    if target:
        return target
    try:
        alb = _elbv2(region).describe_load_balancers(
            Names=[_regional_alb_name(region)]
        )["LoadBalancers"][0]
        return alb.get("DNSName", "")
    except Exception:
        return ""


def _ensure_gateway_origin_record(region: str, log=print):
    """Keep the Cloudflare LB origin hostname resolvable.

    The apex Cloudflare load balancer points each regional pool at
    lb-<abbrev>.appmesh.org. If that CNAME is deleted out-of-band, Cloudflare
    reports 1016/Unknown host even though the gateway ALB and landing backend are
    healthy. Recreate/repoint it whenever LB pools are reconciled.
    """
    if not _cloudflare_enabled():
        return
    dep = _active_gateway_deployment(region)
    if not dep:
        log(f"Skipping gateway origin DNS for {region}: no active gateway deployment")
        return
    res = dep.setdefault("resources", {})
    host = res.get("dns_fqdn") or _cf_gateway_origin_host(region)
    target = _gateway_alb_dns(region)
    if not target:
        log(f"Skipping gateway origin DNS for {region}: regional ALB DNS not found")
        return
    before = res.get("cloudflare_record_id")
    _ensure_cf_record(host, target, res)
    if res.get("cloudflare_record_id") != before or res.get("dns_fqdn") != host:
        res["dns_fqdn"] = host
        dep["resources"] = res
        _save_deployment_to_s3(dep)
    log(f"Gateway origin DNS ready: {host} -> {target}")


def _cf_gateway_pool_payload(region: str, monitor_id: str) -> dict:
    lat, lon = CF_REGION_COORDS.get(region, (0.0, 0.0))
    host = _cf_gateway_origin_host(region)
    return {
        "name": _cf_pool_name(region),
        "description": f"AppMesh gateway pool for {region}",
        "enabled": True,
        "check_regions": CF_LB_CHECK_REGIONS,
        "minimum_origins": 1,
        "monitor": monitor_id,
        "latitude": lat,
        "longitude": lon,
        "origins": [{
            "name": f"gateway-{REGION_ABBREV[region]}",
            "address": host,
            "port": 80,
            "enabled": True,
            "header": {"Host": [CF_LB_NAME]},
        }],
    }


def _cf_ensure_gateway_pool(account_id: str, region: str, monitor_id: str, log=print) -> str:
    _ensure_gateway_origin_record(region, log=log)
    payload = _cf_gateway_pool_payload(region, monitor_id)
    existing = _cf_find_named(_cf_list_pools(account_id), payload["name"])
    if existing:
        _cf_api("PUT", f"/accounts/{account_id}/load_balancers/pools/{existing['id']}",
                json=payload)
        return existing["id"]
    log(f"Creating Cloudflare origin pool {payload['name']} → {payload['origins'][0]['address']}…")
    return _cf_api("POST", f"/accounts/{account_id}/load_balancers/pools",
                   json=payload)["id"]


def _ordered_regions(regions: list[str]) -> list[str]:
    def key(region):
        try:
            return (GATEWAY_REGION_ORDER.index(region), region)
        except ValueError:
            return (999, region)
    return sorted(set(regions), key=key)


def _cf_apex_lb_payload(pool_ids: list[str]) -> dict:
    return {
        "name": CF_LB_NAME,
        "description": "AppMesh public gateway load balancer",
        "enabled": True,
        "proxied": True,
        "steering_policy": CF_LB_STEERING_POLICY,
        "session_affinity": "none",
        "default_pools": pool_ids,
        "fallback_pool": pool_ids[0],
        "location_strategy": {"mode": "pop", "prefer_ecs": "proximity"},
        "adaptive_routing": {"failover_across_pools": True},
    }


def _cf_ensure_apex_load_balancer(regions: list[str] | None = None, log=print):
    """Ensure appmesh.org points at one regional gateway pool per active region."""
    if not _cloudflare_enabled():
        return
    regions = _ordered_regions(regions or _load_active_regions())
    if not regions:
        return
    if len(regions) > MAX_ONBOARDED_REGIONS:
        raise RuntimeError(
            f"Cloudflare plan limit: at most {MAX_ONBOARDED_REGIONS} onboarded regions"
        )
    account_id = _cf_account_id()
    zone_id = CONFIG.get("cloudflare_zone_id", "")
    monitor_id = _cf_ensure_gateway_monitor(account_id, log=log)
    pool_ids = [_cf_ensure_gateway_pool(account_id, r, monitor_id, log=log) for r in regions]
    payload = _cf_apex_lb_payload(pool_ids)
    existing = _cf_find_named(_cf_list_load_balancers(zone_id), CF_LB_NAME)
    if existing:
        log(f"Updating Cloudflare load balancer {CF_LB_NAME} with {len(pool_ids)} pool(s)…")
        _cf_api("PUT", f"/zones/{zone_id}/load_balancers/{existing['id']}", json=payload)
    else:
        log(f"Creating Cloudflare load balancer {CF_LB_NAME} with {len(pool_ids)} pool(s)…")
        _cf_api("POST", f"/zones/{zone_id}/load_balancers", json=payload)


def _cf_delete_gateway_pool(region: str, log=print):
    if not _cloudflare_enabled():
        return
    account_id = _cf_account_id()
    pool = _cf_find_named(_cf_list_pools(account_id), _cf_pool_name(region))
    if pool:
        log(f"Deleting Cloudflare origin pool {_cf_pool_name(region)}…")
        _cf_api("DELETE", f"/accounts/{account_id}/load_balancers/pools/{pool['id']}")


def _cf_delete_lb_assets(log=print):
    """Remove the apex LB, appmesh-* pools, and the shared gateway monitor."""
    if not _cloudflare_enabled():
        return
    zone_id = CONFIG.get("cloudflare_zone_id", "")
    account_id = _cf_account_id()
    lb = _cf_find_named(_cf_list_load_balancers(zone_id), CF_LB_NAME)
    if lb:
        log(f"Deleting Cloudflare load balancer {CF_LB_NAME}…")
        _cf_api("DELETE", f"/zones/{zone_id}/load_balancers/{lb['id']}")
    for pool in _cf_list_pools(account_id):
        if pool.get("name", "").startswith(f"{CF_LB_ORIGIN_PREFIX}-"):
            log(f"Deleting Cloudflare origin pool {pool['name']}…")
            _cf_api("DELETE", f"/accounts/{account_id}/load_balancers/pools/{pool['id']}")
    monitor = _cf_find_named(_cf_list_monitors(account_id), CF_LB_MONITOR_NAME)
    if monitor:
        log(f"Deleting Cloudflare monitor {CF_LB_MONITOR_NAME}…")
        _cf_api("DELETE", f"/accounts/{account_id}/load_balancers/monitors/{monitor['id']}")


def _find_packaged_artifact(config_key: str, bundled_name: str,
                            repo_path: str, build_hint: str) -> str:
    candidates = []
    configured = CONFIG.get(config_key, "").strip()
    if configured:
        candidates.append(Path(configured))
    candidates.append(BUNDLED_JAR_DIR / bundled_name)
    candidates.append(Path(repo_path))

    for path in candidates:
        if path.exists():
            return str(path)

    tried = ", ".join(str(p) for p in candidates)
    raise RuntimeError(f"{build_hint} not found. Tried: {tried}")


def _upload_gateway_jar():
    jar_path = _find_packaged_artifact(
        "gateway_jar_path", GATEWAY_JAR_NAME, GATEWAY_JAR_PATH,
        "Gateway JAR",
    )
    uid = uuid.uuid4().hex[:8]
    jar_name = f"gateway-lb-{uid}.jar"
    key = CONFIG.get("s3_prefix", "jars/") + jar_name
    _s3().upload_file(jar_path, CONFIG["s3_bucket"], key)
    return f"s3://{CONFIG['s3_bucket']}/{key}", key, jar_name


def _upload_landing_jar():
    jar_path = _find_packaged_artifact(
        "landing_jar_path", LANDING_JAR_NAME, LANDING_JAR_PATH,
        "Landing JAR",
    )
    uid = uuid.uuid4().hex[:8]
    jar_name = f"landing-{uid}.jar"
    key = CONFIG.get("s3_prefix", "jars/") + jar_name
    _s3().upload_file(jar_path, CONFIG["s3_bucket"], key)
    return f"s3://{CONFIG['s3_bucket']}/{key}", key, jar_name


def _build_and_upload_console_zip():
    import io
    import zipfile
    src = APP_DIR
    exclude_dirs = {".venv", "uploads", "__pycache__", ".git", "bundled_jars"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in src.rglob("*"):
            if path.is_dir():
                continue
            rel = path.relative_to(src)
            if any(part in exclude_dirs for part in rel.parts):
                continue
            if rel.suffix == ".pyc":
                continue
            if rel.name.startswith("_") and rel.suffix == ".py":
                continue  # local helper scripts
            arc = str(rel).replace("\\", "/")
            if rel.name == "config.properties":
                # Strip static AWS keys — the instance role supplies them on EC2.
                kept = [ln for ln in path.read_text().splitlines()
                        if not ln.strip().startswith(("aws_access_key_id", "aws_secret_access_key"))]
                z.writestr("config.properties", "\n".join(kept) + "\n")
            else:
                z.write(path, arc)
        z.write(
            _find_packaged_artifact("gateway_jar_path", GATEWAY_JAR_NAME,
                                    GATEWAY_JAR_PATH, "Gateway JAR"),
            f"bundled_jars/{GATEWAY_JAR_NAME}",
        )
        z.write(
            _find_packaged_artifact("landing_jar_path", LANDING_JAR_NAME,
                                    LANDING_JAR_PATH, "Landing JAR"),
            f"bundled_jars/{LANDING_JAR_NAME}",
        )
    buf.seek(0)
    uid = uuid.uuid4().hex[:8]
    key = CONFIG.get("s3_prefix", "jars/") + f"console-{uid}.zip"
    _s3().put_object(Bucket=CONFIG["s3_bucket"], Key=key, Body=buf.getvalue())
    return f"s3://{CONFIG['s3_bucket']}/{key}", key


def _register_infra_deployment(name, region, kind, resources):
    """Record an init-provisioned stack as a root-owned deployment (shows in dashboard,
    tears down via the normal flow — shared ALB/role are intentionally left out of resources)."""
    steps = {s: {"status": "done", "detail": "provisioned by initialize_infrastructure",
                 "label": STEP_LABELS[s]} for s in STEP_KEYS}
    td = {s: {"status": "pending", "detail": "", "label": TEARDOWN_STEP_LABELS[s]}
          for s in TEARDOWN_STEP_KEYS}
    dep = {
        "id": f"{name}-{uuid.uuid4().hex[:8]}",
        "name": name,
        "owner": "root",
        "kind": kind,
        "jar_filename": resources.get("artifact", ""),
        "created_at": datetime.utcnow().isoformat(),
        "status": "deployed",
        "region": region,
        "desired_instances": resources.get("asg_desired", 1),
        "health_check_path": resources.get("health_check_path", "/status"),
        "steps": steps,
        "teardown_steps": td,
        "resources": resources,
        "_version": 1,
    }
    DEPLOYMENTS[dep["id"]] = dep
    _save_deployment_to_s3(dep)
    return dep


def _delete_regional_alb(region, log=print):
    """Delete the region's shared ALB + its security group (used when removing a region)."""
    elbv2 = _elbv2(region)
    ec2 = _ec2(region)
    try:
        alb = elbv2.describe_load_balancers(Names=[_regional_alb_name(region)])["LoadBalancers"][0]
    except ClientError:
        return
    arn = alb["LoadBalancerArn"]
    sgs = alb.get("SecurityGroups", [])
    elbv2.delete_load_balancer(LoadBalancerArn=arn)
    for _ in range(60):
        try:
            if not elbv2.describe_load_balancers(LoadBalancerArns=[arn])["LoadBalancers"]:
                break
        except ClientError:
            break
        time.sleep(5)
    time.sleep(10)
    for sg in sgs:
        try:
            ec2.delete_security_group(GroupId=sg)
        except ClientError:
            pass


# ── High-level operations (callable from CLI and console UI) ────────
def deploy_console(log=print):
    log("Ensuring console IAM role…")
    profile = ensure_console_role()
    log(f"Ensuring shared ALB in {CONSOLE_REGION}…")
    _ensure_regional_alb(CONSOLE_REGION)
    log("Building + uploading console package…")
    s3_uri, s3_key = _build_and_upload_console_zip()
    host = f"console.{CONFIG.get('cloudflare_domain', 'appmesh.org')}"
    user_data = (CONSOLE_USER_DATA_TEMPLATE
                 .replace("{s3_uri}", s3_uri)
                 .replace("{s3_bucket}", CONFIG["s3_bucket"]))
    log(f"Provisioning console stack in {CONSOLE_REGION}… (1 × t3.small)")
    res = provision_stack("console", CONSOLE_REGION, profile, user_data,
                          health_check_path="/status", host_fqdn=host,
                          desired=1, instance_type="t3.small")
    res["artifact"] = s3_key.split("/")[-1]
    res["s3_uri"] = s3_uri
    res["s3_key"] = s3_key
    log(f"Creating Cloudflare record {host} → {res['alb_dns']}…")
    _ensure_cf_record(host, res["alb_dns"], res)
    _register_infra_deployment("console", CONSOLE_REGION, "console", res)
    log(f"Console ready: http://{host}")


def redeploy_console(log=print):
    """Roll the existing console ASG to a freshly packaged console zip.

    This is intentionally in-place: the Cloudflare record, shared ALB listener
    rule, target group, and ASG stay the same. Only the launch template user-data
    changes to point at the new console zip in S3, then ASG instance refresh
    replaces the instances.
    """
    consoles = [
        d for d in DEPLOYMENTS.values()
        if d.get("kind") == "console"
        and d.get("status") not in ("torn_down", "teardown_partial")
    ]
    if not consoles:
        log("No existing console deployment found — creating one.")
        return deploy_console(log=log)

    dep = sorted(consoles, key=lambda d: d.get("created_at", ""))[-1]
    region = dep.get("region") or CONSOLE_REGION
    res = dep.get("resources", {})
    if not res.get("launch_template_id") or not res.get("asg_name"):
        raise RuntimeError(f"Console deployment {dep['id']} is missing LT/ASG metadata")

    log("Building + uploading console package…")
    s3_uri, s3_key = _build_and_upload_console_zip()
    user_data = (CONSOLE_USER_DATA_TEMPLATE
                 .replace("{s3_uri}", s3_uri)
                 .replace("{s3_bucket}", CONFIG["s3_bucket"]))

    dep["status"] = "updating"
    _save_deployment_to_s3(dep)

    log("Creating console launch template version…")
    ec2 = _ec2(region)
    new_ver = ec2.create_launch_template_version(
        LaunchTemplateId=res["launch_template_id"],
        SourceVersion="$Latest",
        LaunchTemplateData={"UserData": base64.b64encode(user_data.encode()).decode()},
        VersionDescription=f"Console package update: {Path(s3_key).name}",
    )
    ver_num = new_ver["LaunchTemplateVersion"]["VersionNumber"]
    ec2.modify_launch_template(
        LaunchTemplateId=res["launch_template_id"],
        DefaultVersion=str(ver_num),
    )

    log("Starting console instance refresh…")
    asg_client = _autoscaling(region)
    refresh_id = asg_client.start_instance_refresh(
        AutoScalingGroupName=res["asg_name"],
        Strategy="Rolling",
        Preferences={
            "MinHealthyPercentage": 100,
            "MaxHealthyPercentage": 200,
            "InstanceWarmup": 180,
        },
    )["InstanceRefreshId"]
    res["last_instance_refresh_id"] = refresh_id

    for poll in range(120):
        rr = asg_client.describe_instance_refreshes(
            AutoScalingGroupName=res["asg_name"],
            InstanceRefreshIds=[refresh_id],
        ).get("InstanceRefreshes", [])
        if rr:
            status = rr[0]["Status"]
            pct = rr[0].get("PercentageComplete", 0)
            log(f"  console refresh {status} ({pct}%)")
            if status == "Successful":
                break
            if status in ("Cancelled", "Failed", "RollbackSuccessful", "RollbackFailed"):
                raise RuntimeError(
                    f"Console refresh {status}: {rr[0].get('StatusReason', 'unknown')}"
                )
        time.sleep(15)
    else:
        raise RuntimeError("Console instance refresh timed out")

    elbv2 = _elbv2(region)
    for poll in range(60):
        health = elbv2.describe_target_health(
            TargetGroupArn=res["target_group_arn"]
        ).get("TargetHealthDescriptions", [])
        healthy = [h for h in health if h["TargetHealth"]["State"] == "healthy"]
        log(f"  console targets healthy {len(healthy)}/{len(health)}")
        if health and len(healthy) == len(health):
            break
        time.sleep(10)
    else:
        raise RuntimeError("Console targets did not become healthy")

    res["artifact"] = s3_key.split("/")[-1]
    res["s3_uri"] = s3_uri
    res["s3_key"] = s3_key
    dep["jar_filename"] = res["artifact"]
    dep["resources"] = res
    dep["version"] = dep.get("version", 1) + 1
    dep["version_deployed_at"] = datetime.utcnow().isoformat()
    dep["status"] = "deployed"
    _save_deployment_to_s3(dep)
    log(f"Console redeployed: {dep['id']}")


def redeploy_gateway(region=None, log=print):
    """Roll the gateway ASG(s) to a freshly packaged gateway JAR, in place.

    Mirrors redeploy_console: the Cloudflare record, shared ALB rule, target group
    and ASG are kept; only the launch-template user-data changes to download the new
    gateway JAR from S3, then an ASG instance refresh replaces the instances. Use
    this after rebuilding the gateway JAR (e.g. a routing/config-loader change).
    `region` limits the roll to one gateway region; default rolls every gateway.
    """
    gateways = [
        d for d in DEPLOYMENTS.values()
        if d.get("kind") == "gateway"
        and d.get("status") not in ("torn_down", "teardown_partial")
        and (region is None or d.get("region") == region)
    ]
    if not gateways:
        raise RuntimeError("No gateway deployment found" + (f" in {region}" if region else ""))

    for dep in sorted(gateways, key=lambda d: d.get("region", "")):
        greg = dep.get("region") or CONSOLE_REGION
        res = dep.get("resources", {})
        if not res.get("launch_template_id") or not res.get("asg_name"):
            raise RuntimeError(f"Gateway {dep['id']} is missing LT/ASG metadata")

        log(f"[{greg}] Uploading gateway JAR…")
        s3_uri, s3_key, jar_name = _upload_gateway_jar()
        gw_name = dep.get("name") or f"gateway-{REGION_ABBREV.get(greg, greg)}"
        user_data = (USER_DATA_TEMPLATE
                     .replace("{s3_uri}", s3_uri)
                     .replace("{jar_filename}", jar_name)
                     .replace("{service_name}", gw_name)
                     .replace("{deploy_uid}", uuid.uuid4().hex[:8])
                     .replace("{aws_region}", greg))

        dep["status"] = "updating"
        _save_deployment_to_s3(dep)

        log(f"[{greg}] Creating gateway launch template version…")
        ec2 = _ec2(greg)
        new_ver = ec2.create_launch_template_version(
            LaunchTemplateId=res["launch_template_id"],
            SourceVersion="$Latest",
            LaunchTemplateData={"UserData": base64.b64encode(user_data.encode()).decode()},
            VersionDescription=f"Gateway package update: {jar_name}",
        )
        ver_num = new_ver["LaunchTemplateVersion"]["VersionNumber"]
        ec2.modify_launch_template(
            LaunchTemplateId=res["launch_template_id"],
            DefaultVersion=str(ver_num),
        )

        log(f"[{greg}] Starting gateway instance refresh…")
        asg_client = _autoscaling(greg)
        refresh_id = asg_client.start_instance_refresh(
            AutoScalingGroupName=res["asg_name"],
            Strategy="Rolling",
            Preferences={"MinHealthyPercentage": 100, "MaxHealthyPercentage": 200,
                         "InstanceWarmup": 180},
        )["InstanceRefreshId"]
        res["last_instance_refresh_id"] = refresh_id

        for _poll in range(120):
            rr = asg_client.describe_instance_refreshes(
                AutoScalingGroupName=res["asg_name"],
                InstanceRefreshIds=[refresh_id],
            ).get("InstanceRefreshes", [])
            if rr:
                status = rr[0]["Status"]
                pct = rr[0].get("PercentageComplete", 0)
                log(f"  [{greg}] gateway refresh {status} ({pct}%)")
                if status == "Successful":
                    break
                if status in ("Cancelled", "Failed", "RollbackSuccessful", "RollbackFailed"):
                    raise RuntimeError(
                        f"Gateway refresh {status}: {rr[0].get('StatusReason', 'unknown')}")
            time.sleep(15)
        else:
            raise RuntimeError("Gateway instance refresh timed out")

        elbv2 = _elbv2(greg)
        for _poll in range(60):
            health = elbv2.describe_target_health(
                TargetGroupArn=res["target_group_arn"]
            ).get("TargetHealthDescriptions", [])
            healthy = [h for h in health if h["TargetHealth"]["State"] == "healthy"]
            log(f"  [{greg}] gateway targets healthy {len(healthy)}/{len(health)}")
            if health and len(healthy) == len(health):
                break
            time.sleep(10)
        else:
            raise RuntimeError("Gateway targets did not become healthy")

        res["artifact"] = jar_name
        res["s3_uri"] = s3_uri
        res["s3_key"] = s3_key
        dep["jar_filename"] = jar_name
        dep["resources"] = res
        dep["version"] = dep.get("version", 1) + 1
        dep["version_deployed_at"] = datetime.utcnow().isoformat()
        dep["status"] = "deployed"
        _save_deployment_to_s3(dep)
        log(f"[{greg}] Gateway redeployed: {dep['id']}")

    # Republish the per-region routing config for the rolled gateway(s).
    _sync_gateway_config(log=log)


def deploy_landing(log=print):
    """Deploy the public landing service (the directory at appmesh.org/). It is a
    normal backend service the gateways proxy to, but mapped to the root path "/"
    in the gateway config. Deployed once, in the console region. Idempotent."""
    for dep in DEPLOYMENTS.values():
        if dep.get("kind") == "landing" and dep.get("status") == "deployed":
            log("Landing service already deployed — skipping.")
            return
    region = CONSOLE_REGION
    log("Ensuring landing IAM role…")
    profile = ensure_gateway_role()   # S3 read-only — it only pulls its own JAR
    log(f"Ensuring shared ALB in {region}…")
    _ensure_regional_alb(region)
    log("Uploading landing JAR…")
    s3_uri, s3_key, jar_name = _upload_landing_jar()
    host = _cf_dns_name("landing", region)   # landing-<region>.appmesh.org (gateway backend)
    user_data = (USER_DATA_TEMPLATE
                 .replace("{s3_uri}", s3_uri)
                 .replace("{jar_filename}", jar_name)
                 .replace("{service_name}", "landing")
                 .replace("{deploy_uid}", uuid.uuid4().hex[:8])
                 .replace("{aws_region}", region))
    log(f"Provisioning landing stack in {region}…")
    res = provision_stack("landing", region, profile, user_data,
                          health_check_path="/status", host_fqdn=host)
    res["artifact"] = jar_name
    res["s3_uri"] = s3_uri
    res["s3_key"] = s3_key
    log(f"Creating Cloudflare record {host} → {res['alb_dns']}…")
    _ensure_cf_record(host, res["alb_dns"], res)
    dep = _register_infra_deployment("landing", region, "landing", res)
    dep["health_check_path"] = "/status"
    _save_deployment_to_s3(dep)
    # Publish the landing service into the gateways' routing table (mapped to "/").
    _sync_gateway_config(log=log)
    log(f"Landing ready — appmesh.org/ routes to {host}")


def add_region(region, log=print):
    if region not in SUPPORTED_REGIONS:
        raise RuntimeError(f"Unsupported region: {region}")
    active = _load_active_regions()
    if region in active:
        log(f"Region {region} already active — refreshing Cloudflare load balancer.")
        _cf_ensure_apex_load_balancer(active, log=log)
        return
    if len(active) >= MAX_ONBOARDED_REGIONS:
        raise RuntimeError(
            f"Cloudflare plan limit: at most {MAX_ONBOARDED_REGIONS} onboarded regions"
        )
    abbrev = REGION_ABBREV[region]
    log("Ensuring gateway IAM role…")
    gw_profile = ensure_gateway_role()
    log(f"Ensuring shared ALB in {region}…")
    _ensure_regional_alb(region)
    log("Uploading gateway JAR…")
    s3_uri, s3_key, jar_name = _upload_gateway_jar()
    host = f"lb-{abbrev}.{CONFIG.get('cloudflare_domain', 'appmesh.org')}"
    gw_name = f"gateway-{abbrev}"
    user_data = (USER_DATA_TEMPLATE
                 .replace("{s3_uri}", s3_uri)
                 .replace("{jar_filename}", jar_name)
                 .replace("{service_name}", gw_name)
                 .replace("{deploy_uid}", uuid.uuid4().hex[:8])
                 .replace("{aws_region}", region))
    log(f"Provisioning gateway stack in {region}… (2 × t3.small)")
    res = provision_stack(gw_name, region, gw_profile, user_data,
                          health_check_path="/status", host_fqdn=host,
                          desired=2, instance_type="t3.small")
    res["artifact"] = jar_name
    res["s3_uri"] = s3_uri
    res["s3_key"] = s3_key
    log(f"Creating Cloudflare record {host} → {res['alb_dns']}…")
    _ensure_cf_record(host, res["alb_dns"], res)
    # Every gateway also answers for the bare apex appmesh.org (Host-header rule),
    # so a Cloudflare Load Balancer fronting appmesh.org can steer to any region's
    # gateway. The apex DNS itself is owned by the Cloudflare LB — not a CNAME here.
    domain = CONFIG.get("cloudflare_domain", "appmesh.org")
    res["apex_rule_arn"] = _add_host_rule(region, res["listener_arn"], domain,
                                          res["target_group_arn"])
    log(f"Gateway answers for apex {domain} (Host rule) — Cloudflare LB steers to it")
    # Registering the gateway makes the region "active" (active is derived).
    _register_infra_deployment(gw_name, region, "gateway", res)
    _sync_gateway_config(log=log)
    _cf_ensure_apex_load_balancer(active + [region], log=log)
    log(f"Region {region} onboarded — gateway at http://{host}")


def remove_region(region, log=print):
    if region == CONSOLE_REGION:
        raise RuntimeError("Refusing to remove the console region")
    # Tear down every deployment registered in this region (gateways + any user
    # services). Once the gateway record is gone the region is no longer "active".
    for dep in list(DEPLOYMENTS.values()):
        if dep.get("region") == region:
            log(f"Tearing down {dep['name']}…")
            for err in _teardown_resources_simple(dep):
                log(f"  ! {err}")
            _delete_deployment_from_s3(dep["id"])
            DEPLOYMENTS.pop(dep["id"], None)
    _cf_ensure_apex_load_balancer(_load_active_regions(), log=log)
    _cf_delete_gateway_pool(region, log=log)
    # Regenerate the gateway's S3 routing config without the removed region's services.
    _sync_gateway_config(log=log)
    _delete_gateway_config(region, log=log)
    log(f"Deleting shared ALB in {region}…")
    _delete_regional_alb(region, log=log)
    log(f"Region {region} removed.")


def onboard_service_region(source_dep_id: str, region: str, log=print):
    _load_deployments_from_s3()
    source = DEPLOYMENTS.get(source_dep_id)
    if not source or not _is_user_service(source):
        raise RuntimeError("Service deployment not found")
    if region not in _load_active_regions():
        raise RuntimeError(f"Region {region} is not onboarded")

    service_name = _service_key(source)
    owner = source.get("owner") or "root"
    if region in _service_regions(service_name, owner):
        log(f"{service_name} already exists in {region} — nothing to do.")
        return

    res0 = source.get("resources", {})
    s3_uri = res0.get("s3_uri")
    s3_key = res0.get("s3_key")
    jar_name = res0.get("s3_jar_name") or (s3_key or "").rsplit("/", 1)[-1]
    if not s3_uri or not jar_name:
        raise RuntimeError("Source deployment does not have a reusable S3 JAR artifact")

    dep_id = f"{service_name}-{REGION_ABBREV[region]}-{uuid.uuid4().hex[:8]}"
    now = datetime.utcnow().isoformat()
    host = _cf_dns_name(service_name, region)
    user_data = (USER_DATA_TEMPLATE
                 .replace("{s3_uri}", s3_uri)
                 .replace("{jar_filename}", jar_name)
                 .replace("{service_name}", service_name)
                 .replace("{deploy_uid}", dep_id.rsplit("-", 1)[-1])
                 .replace("{aws_region}", region))

    log(f"Ensuring service instance role for {service_name}…")
    profile = ensure_gateway_role()
    log(f"Provisioning {service_name} in {region}…")
    resources = provision_stack(
        service_name, region, profile, user_data,
        health_check_path=source.get("health_check_path", "/status"),
        host_fqdn=host,
        desired=int(source.get("desired_instances", 1)),
        instance_type=CONFIG.get("instance_type", "t3.micro"),
    )
    resources.update({
        "artifact": source.get("jar_filename", jar_name),
        "s3_uri": s3_uri,
        "s3_key": s3_key,
        "s3_jar_name": jar_name,
    })
    log(f"Creating Cloudflare record {host} → {resources['alb_dns']}…")
    _ensure_cf_record(host, resources["alb_dns"], resources)

    steps = {s: {"status": "done", "detail": "provisioned by service region onboarding",
                 "label": STEP_LABELS[s]} for s in STEP_KEYS}
    dep = {
        "id": dep_id,
        "name": service_name,
        "service_name": service_name,
        "owner": owner,
        "jar_filename": source.get("jar_filename", jar_name),
        "created_at": now,
        "status": "deployed",
        "region": region,
        "desired_instances": int(source.get("desired_instances", 1)),
        "health_check_path": source.get("health_check_path", "/status"),
        "version": source.get("version", 1),
        "version_deployed_at": source.get("version_deployed_at") or now,
        "history": source.get("history", []),
        "steps": steps,
        "resources": resources,
        "_version": 1,
    }
    DEPLOYMENTS[dep_id] = dep
    _save_deployment_to_s3(dep)
    _sync_gateway_config(log=log)
    log(f"{service_name} onboarded in {region}: http://{host}")


def remove_service_region(dep_id: str, log=print):
    _load_deployments_from_s3()
    dep = DEPLOYMENTS.get(dep_id)
    if not dep or not _is_user_service(dep):
        raise RuntimeError("Service deployment not found")
    if len(_service_deployments(_service_key(dep), dep.get("owner") or "root")) <= 1:
        raise RuntimeError("This is the last region for the service; use Teardown to delete the service")
    log(f"Removing {dep['name']} from {dep.get('region')}…")
    for err in _teardown_resources_simple(dep):
        log(f"  ! {err}")
    _delete_deployment_from_s3(dep["id"])
    DEPLOYMENTS.pop(dep["id"], None)
    _sync_gateway_config(log=log)
    log(f"Removed {dep['name']} from {dep.get('region')}.")


def init_all(regions=None, log=print):
    log("Ensuring DynamoDB tables + root user…")
    _ensure_ddb_tables()
    _seed_root_user()
    log("Ensuring IAM roles…")
    ensure_gateway_role()
    ensure_console_role()
    deploy_console(log=log)
    deploy_landing(log=log)
    # Default to onboarding only the console region; pass regions to add more.
    for r in (regions or [CONSOLE_REGION]):
        add_region(r, log=log)
    log("Infrastructure initialization complete.")


def _nuke_region_appmesh(region, log=print):
    """Brute-force delete every appmesh-* AWS resource in a region (ASGs → shared
    ALB → instances → launch templates → target groups → security groups), in
    dependency order. Catches orphans left by a partial/killed provision. Does NOT
    touch DynamoDB tables or IAM roles."""
    ec2 = _ec2(region)
    elbv2 = _elbv2(region)
    asg = _autoscaling(region)
    P = "appmesh-"

    for a in asg.describe_auto_scaling_groups().get("AutoScalingGroups", []):
        n = a["AutoScalingGroupName"]
        if n.startswith(P):
            log(f"delete ASG {n}")
            try:
                asg.delete_auto_scaling_group(AutoScalingGroupName=n, ForceDelete=True)
            except ClientError as e:
                log(f"  ! {e}")

    log("delete shared ALB (if present)…")
    try:
        _delete_regional_alb(region, log=log)
    except Exception as e:
        log(f"  ! {e}")

    log("waiting for appmesh instances to terminate…")
    for _ in range(36):
        live = []
        for r in ec2.describe_instances(Filters=[{"Name": "instance-state-name",
                "Values": ["pending", "running", "stopping", "shutting-down"]}]).get("Reservations", []):
            for i in r["Instances"]:
                nm = next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "Name"), "")
                if nm.startswith(P):
                    live.append(i["InstanceId"])
        if not live:
            break
        log(f"  still terminating: {live}")
        time.sleep(10)

    for lt in ec2.describe_launch_templates().get("LaunchTemplates", []):
        if lt["LaunchTemplateName"].startswith(P):
            log(f"delete LT {lt['LaunchTemplateName']}")
            try:
                ec2.delete_launch_template(LaunchTemplateId=lt["LaunchTemplateId"])
            except ClientError as e:
                log(f"  ! {e}")

    for tg in elbv2.describe_target_groups().get("TargetGroups", []):
        if tg["TargetGroupName"].startswith(P):
            log(f"delete TG {tg['TargetGroupName']}")
            try:
                elbv2.delete_target_group(TargetGroupArn=tg["TargetGroupArn"])
            except ClientError as e:
                log(f"  ! {e}")

    for attempt in range(8):
        remaining = []
        for sg in ec2.describe_security_groups().get("SecurityGroups", []):
            if sg["GroupName"].startswith(P):
                try:
                    ec2.delete_security_group(GroupId=sg["GroupId"])
                    log(f"deleted SG {sg['GroupName']}")
                except ClientError:
                    remaining.append(sg["GroupName"])
        if not remaining:
            break
        log(f"SGs still in use (attempt {attempt + 1}), retrying: {remaining}")
        time.sleep(15)


def _purge_deployment_records(log=print):
    """Delete every deployment item from DynamoDB (control-plane registry only)."""
    _load_deployments_from_s3()
    ids = list(DEPLOYMENTS)
    for dep_id in ids:
        _delete_deployment_from_s3(dep_id)
        DEPLOYMENTS.pop(dep_id, None)
    log(f"purged {len(ids)} deployment record(s) from DynamoDB")


def _purge_infra_cf_records(regions, log=print):
    """Delete the Cloudflare CNAMEs init creates for infra hostnames, in case a
    killed init left them before the deployment was registered (registered ones
    are removed by the per-deployment teardown). Bounded to known names."""
    if not _cloudflare_enabled():
        return
    zone = CONFIG.get("cloudflare_zone_id", "")
    domain = CONFIG.get("cloudflare_domain", "appmesh.org")
    hosts = {f"console.{domain}"}
    for region in regions:
        abbrev = REGION_ABBREV.get(region, region)
        hosts.add(f"lb-{abbrev}.{domain}")
        hosts.add(f"landing-{region}.{domain}")
    for host in sorted(hosts):
        try:
            rec = _cf_find_record(zone, host)
            if rec:
                _cf_delete_record(zone, rec["id"])
                log(f"deleted Cloudflare record {host}")
        except Exception as e:
            log(f"  ! cf {host}: {e}")


def teardown_all(log=print):
    """Delete EVERYTHING this platform created — registered or orphaned. Sweeps
    every supported region for appmesh-* AWS resources, removes Cloudflare DNS,
    and purges the DynamoDB deployment registry. IAM roles + DDB tables are kept."""
    # 1) Graceful, record-driven teardown first (removes each deployment's own
    #    Cloudflare DNS record + AWS resources cleanly).
    for r in [r for r in _load_active_regions() if r != CONSOLE_REGION]:
        remove_region(r, log=log)
    for dep in list(DEPLOYMENTS.values()):
        log(f"Tearing down {dep['name']}…")
        for err in (_teardown_resources_simple(dep) or []):
            log(f"  ! {err}")
        _delete_deployment_from_s3(dep["id"])
        DEPLOYMENTS.pop(dep["id"], None)
    # 2) Brute-force sweep EVERY supported region to catch orphans (resources from
    #    a killed/partial init that were never registered).
    sweep_regions = sorted(set(SUPPORTED_REGIONS) | {CONSOLE_REGION})
    for region in sweep_regions:
        log(f"--- sweeping {region} ---")
        try:
            _nuke_region_appmesh(region, log=log)
        except Exception as e:
            log(f"  ! sweep {region}: {e}")
    # 3) Remove any orphaned infra Cloudflare records + purge DDB stragglers.
    try:
        _cf_delete_lb_assets(log=log)
    except Exception as e:
        log(f"  ! Cloudflare LB cleanup: {e}")
    _purge_infra_cf_records(sweep_regions, log=log)
    _purge_deployment_records(log=log)
    # 4) Remove gateway routing config objects from S3 (legacy global + per-region),
    #    so a fresh init starts from a clean slate.
    try:
        keys = [GATEWAY_CONFIG_LEGACY_KEY] + [_gateway_config_key(r) for r in sweep_regions]
        for k in keys:
            try:
                _s3().delete_object(Bucket=CONFIG["s3_bucket"], Key=k)
            except Exception:
                pass
        log("Removed gateway routing config objects from S3 (legacy + per-region).")
    except Exception as e:
        log(f"  ! gateway config cleanup: {e}")
    log("Teardown complete — all appmesh resources deleted. (IAM roles + DynamoDB tables kept.)")


# ── In-flight infra-op state (for the console UI's live log) ────────
INFRA_OP = {"status": "idle", "title": "", "region": "", "mode": "", "log": []}


def _infra_log(msg):
    line = f"{datetime.utcnow().strftime('%H:%M:%S')}  {msg}"
    INFRA_OP["log"].append(line)
    print(line)


def _run_infra_op(title, fn, region="", mode=""):
    INFRA_OP.update(status="running", title=title, region=region, mode=mode, log=[])
    try:
        fn(log=_infra_log)
        _infra_log("✓ done")
        INFRA_OP["status"] = "done"
    except Exception as e:
        _infra_log(f"✗ ERROR: {e}")
        INFRA_OP["status"] = "error"


# ---------------------------------------------------------------------------
# Authentication & authorization
# ---------------------------------------------------------------------------
PUBLIC_ENDPOINTS = {"login", "logout", "healthz", "status", "static"}


def current_user():
    return session.get("user")


def is_root():
    return session.get("role") == "root"


@app.context_processor
def _inject_nav():
    """Sidebar deployment count chip — cheap in-memory tally of visible deployments."""
    try:
        if not session.get("user"):
            return {}
        if is_root():
            n = len(DEPLOYMENTS)
        else:
            me = current_user()
            n = sum(1 for v in DEPLOYMENTS.values() if (v.get("owner") or "root") == me)
        return {"nav_deploy_count": n}
    except Exception:
        return {"nav_deploy_count": None}


@app.before_request
def _require_login():
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if not session.get("user"):
        if request.path.startswith("/api/"):
            return jsonify({"error": "authentication required"}), 401
        return redirect(url_for("login", next=request.path))
    # Cluster-wide scan, account management, and region ops are root-only.
    p = request.path
    if (p in ("/scan", "/accounts", "/regions")
            or p.startswith("/api/scan")
            or p.startswith("/api/accounts")
            or p.startswith("/api/regions")):
        if not is_root():
            if p.startswith("/api/"):
                return jsonify({"error": "forbidden"}), 403
            return redirect(url_for("index"))
    return None


def _visible_dep(dep_id: str):
    """Return the deployment if the current user may access it, else None."""
    dep = DEPLOYMENTS.get(dep_id)
    if not dep:
        return None
    if is_root() or (dep.get("owner") or "root") == current_user():
        return dep
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user = get_user(username)
        if user and check_password_hash(user.get("password_hash", ""), password):
            session.permanent = True
            session["user"] = username
            session["role"] = user.get("role", "user")
            return redirect(request.args.get("next") or url_for("index"))
        return render_template("login.html", error="Invalid username or password"), 401
    if session.get("user"):
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/healthz")
def healthz():
    return "ok", 200


@app.route("/status")
def status():
    """Health endpoint used by the ALB target group (all services answer on /status)."""
    return jsonify({"status": "UP", "service": "console"}), 200


# ── Account management (root only) ──────────────────────────────────
@app.route("/accounts")
def accounts_page():
    if not is_root():
        return redirect(url_for("index"))
    dep_counts = {}
    for v in DEPLOYMENTS.values():
        if not _is_user_service(v) or v.get("status") == "torn_down":
            continue
        owner = v.get("owner") or "root"
        dep_counts[owner] = dep_counts.get(owner, 0) + 1
    users = [
        {"username": u.get("username"), "role": u.get("role", "user"),
         "created_at": u.get("created_at", ""),
         "deployments": dep_counts.get(u.get("username"), 0)}
        for u in list_users()
    ]
    users.sort(key=lambda u: (u["role"] != "root", u["username"]))
    return render_template("accounts.html", users=users, current_user=current_user())


@app.route("/api/accounts", methods=["POST"])
def api_create_account():
    if not is_root():
        return jsonify({"error": "forbidden"}), 403
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    role = request.form.get("role", "user")
    if not username or not password:
        return jsonify({"error": "username and password are required"}), 400
    if role not in ("user", "root"):
        role = "user"
    try:
        create_user(username, password, role)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return jsonify({"error": "user already exists"}), 409
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/accounts/<username>/delete", methods=["POST"])
def api_delete_account(username):
    if not is_root():
        return jsonify({"error": "forbidden"}), 403
    if username == ROOT_USERNAME:
        return jsonify({"error": "cannot delete the root account"}), 400
    _load_deployments_from_s3()
    owned = [
        d for d in DEPLOYMENTS.values()
        if (d.get("owner") or "root") == username
        and _is_user_service(d)
        and d.get("status") != "torn_down"
    ]
    if owned:
        names = ", ".join(sorted({d.get("name") or d.get("id") for d in owned}))
        return jsonify({
            "error": f"cannot delete account with {len(owned)} deployment(s): {names}. Teardown or transfer them first."
        }), 409
    delete_user(username)
    return jsonify({"ok": True})


# ── Region management (root only) ───────────────────────────────────
@app.route("/regions")
def regions_page():
    _load_deployments_from_s3()
    active = _load_active_regions()
    available = [r for r in SUPPORTED_REGIONS if r not in active]
    # Enrich each active region with its gateway + service tally for the card grid.
    gw_by_region, svc_by_region = {}, {}
    for v in DEPLOYMENTS.values():
        r = v.get("region")
        if v.get("kind") == "gateway":
            gw_by_region[r] = v
        elif v.get("kind") not in ("gateway", "console"):
            svc_by_region[r] = svc_by_region.get(r, 0) + 1
    active_cards = [{
        "id": r,
        "console": r == CONSOLE_REGION,
        "gateway": (gw_by_region.get(r) or {}).get("name"),
        "services": svc_by_region.get(r, 0),
        "onboarded": (gw_by_region.get(r) or {}).get("created_at", "")[:10],
    } for r in active]
    return render_template("regions.html", active=active_cards, available=available,
                           region_labels=REGION_LABELS,
                           console_region=CONSOLE_REGION, op=INFRA_OP,
                           max_regions=MAX_ONBOARDED_REGIONS,
                           at_region_limit=len(active) >= MAX_ONBOARDED_REGIONS)


@app.route("/api/regions", methods=["GET"])
def api_regions():
    active = _load_active_regions()
    log = INFRA_OP.get("log") or []
    return jsonify({
        "active": active,
        "available": [r for r in SUPPORTED_REGIONS if r not in active],
        "console_region": CONSOLE_REGION,
        "max_regions": MAX_ONBOARDED_REGIONS,
        "op": {
            "status": INFRA_OP["status"],
            "title": INFRA_OP["title"],
            "region": INFRA_OP.get("region", ""),
            "mode": INFRA_OP.get("mode", ""),
            "last_line": log[-1] if log else "",
            "log": log[-80:],
        },
    })


@app.route("/api/regions/<region>/add", methods=["POST"])
def api_add_region(region):
    if region not in SUPPORTED_REGIONS:
        return jsonify({"error": "unsupported region"}), 400
    active = _load_active_regions()
    if region not in active and len(active) >= MAX_ONBOARDED_REGIONS:
        return jsonify({"error": f"Cloudflare plan limit: at most {MAX_ONBOARDED_REGIONS} onboarded regions"}), 409
    if INFRA_OP["status"] == "running":
        return jsonify({"error": "another infrastructure operation is in progress"}), 409
    threading.Thread(
        target=_run_infra_op,
        args=(f"Onboarding {region}", lambda log: add_region(region, log=log)),
        kwargs={"region": region, "mode": "add"},
        daemon=True,
    ).start()
    return jsonify({"status": "started"})


@app.route("/api/regions/<region>/remove", methods=["POST"])
def api_remove_region(region):
    if region == CONSOLE_REGION:
        return jsonify({"error": "cannot remove the console region"}), 400
    if INFRA_OP["status"] == "running":
        return jsonify({"error": "another infrastructure operation is in progress"}), 409
    threading.Thread(
        target=_run_infra_op,
        args=(f"Removing {region}", lambda log: remove_region(region, log=log)),
        kwargs={"region": region, "mode": "remove"},
        daemon=True,
    ).start()
    return jsonify({"status": "started"})


@app.route("/api/regions/op/stream")
def api_regions_op_stream():
    def generate():
        sent = 0
        while True:
            time.sleep(1)
            log = INFRA_OP["log"]
            while sent < len(log):
                yield f"data: {json.dumps({'line': log[sent]})}\n\n"
                sent += 1
            if INFRA_OP["status"] in ("done", "error", "idle"):
                yield f"data: {json.dumps({'status': INFRA_OP['status'], 'region': INFRA_OP.get('region', ''), 'mode': INFRA_OP.get('mode', '')})}\n\n"
                break
    return Response(stream_with_context(generate()), mimetype="text/event-stream")


# ── Service regional membership ─────────────────────────────────────
@app.route("/api/deployments/<dep_id>/regions/<region>/add", methods=["POST"])
def api_onboard_service_region(dep_id, region):
    dep = _visible_dep(dep_id)
    if not dep or not _is_user_service(dep):
        return jsonify({"error": "not found"}), 404
    if region not in _load_active_regions():
        return jsonify({"error": "region is not onboarded"}), 400
    if region in _service_regions(_service_key(dep), dep.get("owner") or "root"):
        return jsonify({"error": "service already exists in that region"}), 409
    if INFRA_OP["status"] == "running":
        return jsonify({"error": "another infrastructure operation is in progress"}), 409
    threading.Thread(
        target=_run_infra_op,
        args=(f"Onboarding {_service_key(dep)} in {region}",
              lambda log: onboard_service_region(dep_id, region, log=log)),
        kwargs={"region": region, "mode": "service-add"},
        daemon=True,
    ).start()
    return jsonify({"status": "started"})


@app.route("/api/deployments/<dep_id>/regions/remove", methods=["POST"])
def api_remove_service_region(dep_id):
    dep = _visible_dep(dep_id)
    if not dep or not _is_user_service(dep):
        return jsonify({"error": "not found"}), 404
    if len(_service_deployments(_service_key(dep), dep.get("owner") or "root")) <= 1:
        return jsonify({"error": "cannot remove the last service region; use Teardown"}), 400
    if INFRA_OP["status"] == "running":
        return jsonify({"error": "another infrastructure operation is in progress"}), 409
    threading.Thread(
        target=_run_infra_op,
        args=(f"Removing {_service_key(dep)} from {dep.get('region')}",
              lambda log: remove_service_region(dep_id, log=log)),
        kwargs={"region": dep.get("region", ""), "mode": "service-remove"},
        daemon=True,
    ).start()
    return jsonify({"status": "started"})


# ---------------------------------------------------------------------------
# Routes – pages
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    # Reload deployments on every dashboard visit so the list is fresh
    _load_deployments_from_s3()
    if is_root():
        visible = dict(DEPLOYMENTS)
    else:
        me = current_user()
        visible = {k: v for k, v in DEPLOYMENTS.items() if (v.get("owner") or "root") == me}

    # Regions strip: one chip per onboarded (active) region, with a service count.
    active = _load_active_regions()
    svc_counts = {}
    for v in visible.values():
        if v.get("kind") in ("gateway", "console"):
            continue
        r = v.get("region")
        svc_counts[r] = svc_counts.get(r, 0) + 1
    regions = [
        {"id": r, "active": True, "services": svc_counts.get(r, 0)}
        for r in active
    ]
    return render_template(
        "index.html", deployments=visible, regions=regions,
        is_root=is_root(), current_user=current_user(),
    )


@app.route("/new")
def new_deployment_page():
    return render_template("new_deployment.html", regions=_load_active_regions())


@app.route("/scan")
def scan_page():
    return render_template("scan.html")


@app.route("/deployment/<dep_id>")
def deployment_detail(dep_id):
    # Re-read from DynamoDB on every page load so we always have fresh data
    try:
        item = _deployments_table().get_item(Key={"dep_id": dep_id}).get("Item")
        if item:
            fresh = json.loads(item["data"])
            fresh.setdefault("_version", 0)
            fresh.setdefault("owner", item.get("owner", "root"))
            existing = DEPLOYMENTS.get(dep_id, {})
            # Don't overwrite in-flight thread state
            if existing.get("status") not in ("deploying", "tearing_down", "updating"):
                DEPLOYMENTS[dep_id] = fresh
    except Exception:
        pass  # fall through to in-memory copy

    dep = _visible_dep(dep_id)
    if not dep:
        return redirect(url_for("index"))
    service = _service_summary(dep) if _is_user_service(dep) else None
    return render_template("deployment_detail.html", dep=dep, service=service)


# ---------------------------------------------------------------------------
# Routes – API
# ---------------------------------------------------------------------------
@app.route("/api/deployments", methods=["GET"])
def api_list_deployments():
    if is_root():
        return jsonify(list(DEPLOYMENTS.values()))
    me = current_user()
    return jsonify([d for d in DEPLOYMENTS.values() if (d.get("owner") or "root") == me])


@app.route("/api/deploy", methods=["POST"])
def api_deploy():
    """Add a service: accept the JAR + config, start the first deployment, return ID."""
    jar = request.files.get("jar")
    if not jar or not jar.filename.endswith(".jar"):
        return jsonify({"error": "A .jar file is required"}), 400

    # Service name is set explicitly at creation (NOT derived from the JAR). It is
    # the public URL prefix appmesh.org/<service>/ and the gateway map key, so it
    # must be a DNS-safe slug and globally unique across all tenants.
    raw_name = (request.form.get("name") or "").strip()
    if not raw_name:
        return jsonify({"error": "A service name is required"}), 400
    service_name = _safe_service_name(raw_name)
    if raw_name != service_name or not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", service_name or ""):
        return jsonify({"error": "Service name must be lowercase letters, digits and "
                                 "hyphens (e.g. police-service)"}), 400

    # Global uniqueness — a live service already owns this name/prefix.
    for d in DEPLOYMENTS.values():
        if d.get("kind") in ("gateway", "console"):
            continue
        if d.get("status") in ("torn_down",):
            continue
        if _safe_service_name(d.get("name", "")) == service_name:
            return jsonify({"error": f"Service name '{service_name}' is already taken"}), 409

    dep_name = service_name
    dep_id = dep_name + "-" + uuid.uuid4().hex[:8]

    # Region(s) & desired instance count. A service can be created in one OR MORE
    # onboarded regions at once: the first is the primary (gets the full deploy
    # pipeline + JAR upload); the rest are onboarded from that same S3 artifact.
    active_regions = _load_active_regions()
    raw_regions = request.form.get("regions")
    if raw_regions:
        selected_regions = [r.strip() for r in raw_regions.split(",") if r.strip()]
    else:  # backward-compatible single-region field
        single = request.form.get("region")
        selected_regions = [single.strip()] if single and single.strip() else []
    # De-duplicate, preserving order (first selected = primary).
    seen: set[str] = set()
    selected_regions = [r for r in selected_regions if not (r in seen or seen.add(r))]
    if not selected_regions:
        return jsonify({"error": "Select at least one region"}), 400
    for r in selected_regions:
        if r not in active_regions:
            return jsonify({"error": f"Region '{r}' not onboarded. Active: {', '.join(active_regions)}"}), 400
    region = selected_regions[0]
    extra_regions = selected_regions[1:]
    try:
        desired_instances = int(request.form.get("instances", "1"))
        if desired_instances < 0:
            raise ValueError
    except (ValueError, TypeError):
        return jsonify({"error": "instances must be a non-negative integer"}), 400

    # Health-check path the shared ALB target group AND the gateway use to probe
    # the service. Every AppMesh service answers on /status by convention.
    health_check_path = (request.form.get("health_check_path") or "/status").strip() or "/status"
    if not health_check_path.startswith("/"):
        health_check_path = "/" + health_check_path

    safe_name = secure_filename(jar.filename)
    if not safe_name or not safe_name.endswith(".jar"):
        return jsonify({"error": "Invalid filename"}), 400
    local_path = UPLOAD_FOLDER / safe_name
    jar.save(str(local_path))

    # Initialize step state as pending
    steps_init = {}
    for s in STEP_KEYS:
        steps_init[s] = {"status": "pending", "detail": "", "label": STEP_LABELS[s]}

    now = datetime.utcnow().isoformat()
    dep = {
        "id": dep_id,
        "name": dep_name,
        "service_name": service_name,
        "kind": "service",
        "owner": current_user() or "root",
        "jar_filename": jar.filename,
        "local_path": str(local_path),
        "created_at": now,
        "status": "deploying",
        "region": region,
        "desired_instances": desired_instances,
        "health_check_path": health_check_path,
        "version": 1,                 # current active JAR version
        "version_deployed_at": now,   # when the active version went live
        "history": [],                # prior versions (most-recent first, cap 5)
        "steps": steps_init,
        "resources": {},
        "_version": 0,
    }
    DEPLOYMENTS[dep_id] = dep

    # Start deployment in background thread (primary region first; any additional
    # selected regions are onboarded from the same S3 JAR once it is healthy).
    t = threading.Thread(target=_run_deployment, args=(dep_id,),
                         kwargs={"extra_regions": extra_regions}, daemon=True)
    t.start()

    return jsonify({"id": dep_id, "regions": selected_regions})


@app.route("/api/deploy/<dep_id>/stream")
def api_deploy_stream(dep_id):
    """SSE endpoint – polls in-memory state and streams changes."""
    dep = _visible_dep(dep_id)
    if not dep:
        return jsonify({"error": "not found"}), 404

    def generate():
        last_version = -1

        # Send initial state immediately
        yield _sse({
            "type": "init",
            "steps": [{"key": s, "label": STEP_LABELS[s]} for s in STEP_KEYS]
        })

        # Send current state of all steps (catchup for refresh)
        for s in STEP_KEYS:
            info = dep["steps"].get(s, {})
            yield _sse({
                "step": s,
                "label": info.get("label", STEP_LABELS[s]),
                "status": info.get("status", "pending"),
                "detail": info.get("detail", ""),
            })
        last_version = dep.get("_version", 0)

        # If already finished, send complete/error immediately
        if dep["status"] == "deployed":
            yield _sse({"type": "complete", "dns": dep["resources"].get("alb_dns", ""), "resources": dep["resources"]})
            return
        if dep["status"] == "failed":
            yield _sse({"type": "error", "message": dep.get("_error", "Unknown error")})
            return

        # Poll for updates until done
        while dep["status"] == "deploying":
            time.sleep(0.5)
            current_version = dep.get("_version", 0)
            if current_version != last_version:
                # Send current state of all steps
                for s in STEP_KEYS:
                    info = dep["steps"].get(s, {})
                    yield _sse({
                        "step": s,
                        "label": info.get("label", STEP_LABELS[s]),
                        "status": info.get("status", "pending"),
                        "detail": info.get("detail", ""),
                    })
                last_version = current_version

        # Final state
        if dep["status"] == "deployed":
            # Send final step states
            for s in STEP_KEYS:
                info = dep["steps"].get(s, {})
                yield _sse({
                    "step": s,
                    "label": info.get("label", STEP_LABELS[s]),
                    "status": info.get("status", "pending"),
                    "detail": info.get("detail", ""),
                })
            yield _sse({"type": "complete", "dns": dep["resources"].get("alb_dns", ""), "resources": dep["resources"]})
        elif dep["status"] == "failed":
            for s in STEP_KEYS:
                info = dep["steps"].get(s, {})
                yield _sse({
                    "step": s,
                    "label": info.get("label", STEP_LABELS[s]),
                    "status": info.get("status", "pending"),
                    "detail": info.get("detail", ""),
                })
            yield _sse({"type": "error", "message": dep.get("_error", "Unknown error")})

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/deployments/<dep_id>/teardown", methods=["POST"])
def api_teardown(dep_id):
    """Start teardown in background, return immediately."""
    dep = _visible_dep(dep_id)
    if not dep:
        return jsonify({"error": "not found"}), 404

    if dep.get("status") == "tearing_down":
        return jsonify({"status": "already_running"})

    dep["status"] = "tearing_down"
    # Initialize teardown steps
    td_steps = {}
    for s in TEARDOWN_STEP_KEYS:
        td_steps[s] = {"status": "pending", "detail": "", "label": TEARDOWN_STEP_LABELS[s]}
    dep["teardown_steps"] = td_steps
    dep["_td_version"] = 0
    _save_deployment_to_s3(dep)  # persist so refresh/restart sees tearing_down state

    t = threading.Thread(target=_run_teardown, args=(dep_id,), daemon=True)
    t.start()

    return jsonify({"status": "started"})


@app.route("/api/deployments/<dep_id>/teardown/stream")
def api_teardown_stream(dep_id):
    """SSE endpoint for teardown progress."""
    dep = _visible_dep(dep_id)
    if not dep:
        return jsonify({"error": "not found"}), 404

    def generate():
        last_version = -1

        yield _sse({
            "type": "init",
            "steps": [{"key": s, "label": TEARDOWN_STEP_LABELS[s]} for s in TEARDOWN_STEP_KEYS],
        })

        # Catchup: send current state of all steps
        td_steps = dep.get("teardown_steps", {})
        for s in TEARDOWN_STEP_KEYS:
            info = td_steps.get(s, {})
            yield _sse({
                "step": s,
                "label": info.get("label", TEARDOWN_STEP_LABELS[s]),
                "status": info.get("status", "pending"),
                "detail": info.get("detail", ""),
            })
        last_version = dep.get("_td_version", 0)

        # Already finished?
        if dep["status"] in ("torn_down", "teardown_partial"):
            yield _sse({"type": "td_complete", "status": dep["status"]})
            return

        # Poll for updates
        while dep["status"] == "tearing_down":
            time.sleep(0.5)
            current_version = dep.get("_td_version", 0)
            if current_version != last_version:
                td_steps = dep.get("teardown_steps", {})
                for s in TEARDOWN_STEP_KEYS:
                    info = td_steps.get(s, {})
                    yield _sse({
                        "step": s,
                        "label": info.get("label", TEARDOWN_STEP_LABELS[s]),
                        "status": info.get("status", "pending"),
                        "detail": info.get("detail", ""),
                    })
                last_version = current_version

        # Final state
        td_steps = dep.get("teardown_steps", {})
        for s in TEARDOWN_STEP_KEYS:
            info = td_steps.get(s, {})
            yield _sse({
                "step": s,
                "label": info.get("label", TEARDOWN_STEP_LABELS[s]),
                "status": info.get("status", "pending"),
                "detail": info.get("detail", ""),
            })
        yield _sse({"type": "td_complete", "status": dep["status"]})

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Routes – JAR update
# ---------------------------------------------------------------------------
@app.route("/api/deployments/<dep_id>/update", methods=["POST"])
def api_update_jar(dep_id):
    """Accept a new JAR, start update (new LT version + instance refresh)."""
    dep = _visible_dep(dep_id)
    if not dep:
        return jsonify({"error": "not found"}), 404

    if dep.get("status") not in ("deployed",):
        return jsonify({"error": f"Cannot update while status is '{dep.get('status')}'"}), 400

    jar = request.files.get("jar")
    if not jar or not jar.filename.endswith(".jar"):
        return jsonify({"error": "A .jar file is required"}), 400

    safe_name = secure_filename(jar.filename)
    if not safe_name or not safe_name.endswith(".jar"):
        return jsonify({"error": "Invalid filename"}), 400
    local_path = UPLOAD_FOLDER / safe_name
    jar.save(str(local_path))

    # Initialize update step state
    upd_steps = {}
    for s in UPDATE_STEP_KEYS:
        upd_steps[s] = {"status": "pending", "detail": "", "label": UPDATE_STEP_LABELS[s]}
    dep["update_steps"] = upd_steps
    dep["_upd_version"] = 0
    dep["_update_jar_filename"] = jar.filename
    dep["_update_local_path"] = str(local_path)
    dep["_update_error"] = ""

    t = threading.Thread(target=_run_service_update, args=(dep_id,), daemon=True)
    t.start()

    return jsonify({"id": dep_id, "status": "started"})


@app.route("/api/deployments/<dep_id>/update/stream")
def api_update_stream(dep_id):
    """SSE endpoint for JAR-update progress."""
    dep = _visible_dep(dep_id)
    if not dep:
        return jsonify({"error": "not found"}), 404

    def generate():
        last_version = -1

        yield _sse({
            "type": "init",
            "steps": [{"key": s, "label": UPDATE_STEP_LABELS[s]} for s in UPDATE_STEP_KEYS],
        })

        # Catchup: send current state of all steps
        upd_steps = dep.get("update_steps", {})
        for s in UPDATE_STEP_KEYS:
            info = upd_steps.get(s, {})
            yield _sse({
                "step": s,
                "label": info.get("label", UPDATE_STEP_LABELS[s]),
                "status": info.get("status", "pending"),
                "detail": info.get("detail", ""),
            })
        last_version = dep.get("_upd_version", 0)

        # Already finished?
        if dep["status"] == "deployed" and any(
            upd_steps.get(s, {}).get("status") == "done" for s in UPDATE_STEP_KEYS
        ):
            error = dep.get("_update_error", "")
            if error:
                yield _sse({"type": "update_error", "message": error})
            else:
                yield _sse({"type": "update_complete"})
            return

        # Poll for updates
        while dep["status"] == "updating":
            time.sleep(0.5)
            current_version = dep.get("_upd_version", 0)
            if current_version != last_version:
                upd_steps = dep.get("update_steps", {})
                for s in UPDATE_STEP_KEYS:
                    info = upd_steps.get(s, {})
                    yield _sse({
                        "step": s,
                        "label": info.get("label", UPDATE_STEP_LABELS[s]),
                        "status": info.get("status", "pending"),
                        "detail": info.get("detail", ""),
                    })
                last_version = current_version

        # Final state
        upd_steps = dep.get("update_steps", {})
        for s in UPDATE_STEP_KEYS:
            info = upd_steps.get(s, {})
            yield _sse({
                "step": s,
                "label": info.get("label", UPDATE_STEP_LABELS[s]),
                "status": info.get("status", "pending"),
                "detail": info.get("detail", ""),
            })

        error = dep.get("_update_error", "")
        if error:
            yield _sse({"type": "update_error", "message": error})
        else:
            yield _sse({"type": "update_complete"})

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Routes – Live status (poll AWS directly)
# ---------------------------------------------------------------------------
@app.route("/api/deployments/<dep_id>/live-status")
def api_live_status(dep_id):
    """Query AWS APIs directly to get the real-time state of every resource."""
    dep = _visible_dep(dep_id)
    if not dep:
        return jsonify({"error": "not found"}), 404

    res = dep.get("resources", {})
    region = dep.get("region") or CONFIG.get("aws_region", "us-east-1")
    live = {}

    # ── Security Group ──────────────────────────────────────
    sg_id = res.get("security_group_id")
    if sg_id:
        try:
            sg_resp = _ec2(region).describe_security_groups(GroupIds=[sg_id])
            sg = sg_resp["SecurityGroups"][0]
            live["security_group"] = {
                "status": "active",
                "id": sg_id,
                "name": sg.get("GroupName", ""),
                "vpc": sg.get("VpcId", ""),
            }
        except Exception:
            live["security_group"] = {"status": "missing", "id": sg_id}

    # ── IAM Role ────────────────────────────────────────────
    role_name = res.get("iam_role_name")
    if role_name:
        try:
            role_resp = _iam(region).get_role(RoleName=role_name)
            live["iam_role"] = {
                "status": "active",
                "name": role_name,
                "arn": role_resp["Role"]["Arn"],
            }
        except Exception:
            live["iam_role"] = {"status": "missing", "name": role_name}

    # ── Instance Profile ────────────────────────────────────
    ip_name = res.get("instance_profile_name")
    if ip_name:
        try:
            ip_resp = _iam(region).get_instance_profile(InstanceProfileName=ip_name)
            roles = [r["RoleName"] for r in ip_resp["InstanceProfile"].get("Roles", [])]
            live["instance_profile"] = {
                "status": "active",
                "name": ip_name,
                "arn": ip_resp["InstanceProfile"]["Arn"],
                "roles": roles,
            }
        except Exception:
            live["instance_profile"] = {"status": "missing", "name": ip_name}

    # ── S3 JAR ──────────────────────────────────────────────
    s3_uri = res.get("s3_uri")
    if s3_uri:
        try:
            s3_key = res.get("s3_key") or (CONFIG.get("s3_prefix", "jars/") + dep["jar_filename"])
            head = _s3().head_object(Bucket=CONFIG["s3_bucket"], Key=s3_key)
            size_mb = round(head["ContentLength"] / (1024 * 1024), 2)
            live["s3_jar"] = {
                "status": "active",
                "uri": s3_uri,
                "size_mb": size_mb,
                "last_modified": str(head.get("LastModified", "")),
            }
        except Exception:
            live["s3_jar"] = {"status": "missing", "uri": s3_uri}

    # ── Launch Template ─────────────────────────────────────
    lt_id = res.get("launch_template_id")
    if lt_id:
        try:
            lt_resp = _ec2(region).describe_launch_templates(LaunchTemplateIds=[lt_id])
            lt = lt_resp["LaunchTemplates"][0]
            live["launch_template"] = {
                "status": "active",
                "id": lt_id,
                "name": lt.get("LaunchTemplateName", ""),
                "version": lt.get("LatestVersionNumber", 1),
            }
        except Exception:
            live["launch_template"] = {"status": "missing", "id": lt_id}

    # ── Target Group ────────────────────────────────────────
    tg_arn = res.get("target_group_arn")
    if tg_arn:
        try:
            elbv2 = _elbv2(region)
            tg_resp = elbv2.describe_target_groups(TargetGroupArns=[tg_arn])
            tg = tg_resp["TargetGroups"][0]
            # Get target health
            health_resp = elbv2.describe_target_health(TargetGroupArn=tg_arn)
            targets = health_resp.get("TargetHealthDescriptions", [])
            healthy_count = sum(1 for t in targets if t.get("TargetHealth", {}).get("State") == "healthy")
            live["target_group"] = {
                "status": "active",
                "name": tg.get("TargetGroupName", ""),
                "arn": tg_arn,
                "port": tg.get("Port"),
                "protocol": tg.get("Protocol"),
                "targets_total": len(targets),
                "targets_healthy": healthy_count,
                "targets": [
                    {
                        "id": t["Target"]["Id"],
                        "port": t["Target"].get("Port"),
                        "health": t.get("TargetHealth", {}).get("State", "unknown"),
                        "reason": t.get("TargetHealth", {}).get("Reason", ""),
                    }
                    for t in targets
                ],
            }
        except Exception:
            live["target_group"] = {"status": "missing", "arn": tg_arn}

    # ── ALB ─────────────────────────────────────────────────
    alb_arn = res.get("alb_arn")
    if alb_arn:
        try:
            alb_resp = _elbv2(region).describe_load_balancers(LoadBalancerArns=[alb_arn])
            alb = alb_resp["LoadBalancers"][0]
            live["alb"] = {
                "status": alb["State"]["Code"],  # active, provisioning, etc.
                "name": alb.get("LoadBalancerName", ""),
                "dns": alb.get("DNSName", ""),
                "scheme": alb.get("Scheme", ""),
                "type": alb.get("Type", ""),
                "azs": [az["ZoneName"] for az in alb.get("AvailabilityZones", [])],
            }
        except Exception:
            live["alb"] = {"status": "missing", "arn": alb_arn}

    # ── Listener ────────────────────────────────────────────
    listener_arn = res.get("listener_arn")
    if listener_arn:
        try:
            l_resp = _elbv2(region).describe_listeners(ListenerArns=[listener_arn])
            lis = l_resp["Listeners"][0]
            live["listener"] = {
                "status": "active",
                "arn": listener_arn,
                "port": lis.get("Port"),
                "protocol": lis.get("Protocol"),
            }
        except Exception:
            live["listener"] = {"status": "missing", "arn": listener_arn}

    # ── ASG ─────────────────────────────────────────────────
    asg_name = res.get("asg_name")
    if asg_name:
        try:
            asg_resp = _autoscaling(region).describe_auto_scaling_groups(
                AutoScalingGroupNames=[asg_name]
            )
            if asg_resp["AutoScalingGroups"]:
                asg = asg_resp["AutoScalingGroups"][0]
                instances = asg.get("Instances", [])
                live["asg"] = {
                    "status": "active",
                    "name": asg_name,
                    "desired": asg["DesiredCapacity"],
                    "min": asg["MinSize"],
                    "max": asg["MaxSize"],
                    "instances_total": len(instances),
                    "instances_healthy": sum(
                        1 for i in instances if i.get("HealthStatus") == "Healthy"
                    ),
                    "azs": asg.get("AvailabilityZones", []),
                    "instances": [
                        {
                            "id": i["InstanceId"],
                            "az": i["AvailabilityZone"],
                            "lifecycle": i["LifecycleState"],
                            "health": i["HealthStatus"],
                        }
                        for i in instances
                    ],
                }
            else:
                live["asg"] = {"status": "missing", "name": asg_name}
        except Exception:
            live["asg"] = {"status": "missing", "name": asg_name}

    # ── DNS Record (Cloudflare) ─────────────────────────────
    dns_fqdn = res.get("dns_fqdn")
    cf_record_id = res.get("cloudflare_record_id")
    if dns_fqdn:
        try:
            cf_zone_id = CONFIG.get("cloudflare_zone_id", "")
            record = _cf_find_record(cf_zone_id, dns_fqdn)
            if record:
                live["dns_record"] = {
                    "status": "active",
                    "fqdn": dns_fqdn,
                    "target": record.get("content", ""),
                    "proxied": record.get("proxied", False),
                    "ttl": record.get("ttl"),
                    "record_id": record.get("id", ""),
                }
            else:
                live["dns_record"] = {"status": "missing", "fqdn": dns_fqdn}
        except Exception:
            live["dns_record"] = {"status": "unknown", "fqdn": dns_fqdn}

    return jsonify(live)


# ---------------------------------------------------------------------------
# Routes – Scaling
# ---------------------------------------------------------------------------
@app.route("/api/deployments/<dep_id>/scale", methods=["GET"])
def api_scale_get(dep_id):
    """Return current ASG desired/min/max capacity and running instance count."""
    dep = _visible_dep(dep_id)
    if not dep:
        return jsonify({"error": "not found"}), 404

    asg_name = dep.get("resources", {}).get("asg_name")
    if not asg_name:
        return jsonify({"error": "No ASG for this deployment"}), 400

    region = dep.get("region") or CONFIG.get("aws_region", "us-east-1")
    try:
        asg_client = _autoscaling(region)
        resp = asg_client.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
        if not resp["AutoScalingGroups"]:
            return jsonify({"error": "ASG not found in AWS"}), 404

        asg = resp["AutoScalingGroups"][0]
        instances = asg.get("Instances", [])
        healthy = [i for i in instances if i.get("HealthStatus") == "Healthy"]

        return jsonify({
            "asg_name": asg_name,
            "desired": asg["DesiredCapacity"],
            "min": asg["MinSize"],
            "max": asg["MaxSize"],
            "running": len(instances),
            "healthy": len(healthy),
            "instances": [
                {
                    "id": i["InstanceId"],
                    "az": i["AvailabilityZone"],
                    "lifecycle": i["LifecycleState"],
                    "health": i["HealthStatus"],
                }
                for i in instances
            ],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/deployments/<dep_id>/scale", methods=["POST"])
def api_scale_set(dep_id):
    """Set the ASG desired capacity. Body: {"desired": N}"""
    dep = _visible_dep(dep_id)
    if not dep:
        return jsonify({"error": "not found"}), 404

    asg_name = dep.get("resources", {}).get("asg_name")
    if not asg_name:
        return jsonify({"error": "No ASG for this deployment"}), 400

    region = dep.get("region") or CONFIG.get("aws_region", "us-east-1")
    body = request.get_json(silent=True) or {}
    desired = body.get("desired")
    if desired is None:
        return jsonify({"error": "'desired' field is required"}), 400

    try:
        desired = int(desired)
    except (ValueError, TypeError):
        return jsonify({"error": "desired must be a valid integer"}), 400
    if desired < 0:
        return jsonify({"error": "desired capacity cannot be negative"}), 400

    try:
        asg_client = _autoscaling(region)

        # Read current ASG to get max
        resp = asg_client.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
        if not resp["AutoScalingGroups"]:
            return jsonify({"error": "ASG not found in AWS"}), 404

        asg = resp["AutoScalingGroups"][0]
        current_max = asg["MaxSize"]

        # If desired > max, raise max to match
        new_max = max(current_max, desired)

        asg_client.update_auto_scaling_group(
            AutoScalingGroupName=asg_name,
            MinSize=0,
            MaxSize=new_max,
            DesiredCapacity=desired,
        )

        return jsonify({
            "ok": True,
            "desired": desired,
            "min": 0,
            "max": new_max,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Scan API endpoints
# ---------------------------------------------------------------------------
@app.route("/api/scan", methods=["POST"])
def api_start_scan():
    """Start a background AWS/Cloudflare scan."""
    if SCAN_RESULT.get("status") == "running":
        return jsonify({"status": "already_running"})
    t = threading.Thread(target=_run_scan, daemon=True)
    t.start()
    return jsonify({"status": "started"})


@app.route("/api/scan/stream")
def api_scan_stream():
    """SSE endpoint for scan progress."""
    def generate():
        last_step_idx = 0
        last_version = -1
        while True:
            current = SCAN_RESULT
            ver = current.get("_version", 0)
            if ver != last_version:
                last_version = ver
                steps = current.get("steps", [])
                # Send new steps
                for i in range(last_step_idx, len(steps)):
                    yield _sse({"type": "step", "step": steps[i]["step"], "detail": steps[i]["detail"]})
                last_step_idx = len(steps)

                if current.get("status") in ("done", "error"):
                    yield _sse({
                        "type": "complete" if current["status"] == "done" else "error",
                        "groups": current.get("groups", {}),
                        "orphan_s3_files": current.get("orphan_s3_files", []),
                        "finished_at": current.get("finished_at"),
                    })
                    return
            time.sleep(0.5)

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/scan/state")
def api_scan_state():
    """Snapshot of the current/last scan so the page can re-attach after navigation,
    reload, or a different user logging in (scan state is global + server-side).
    Running → client re-opens the stream; done → client renders results immediately."""
    return jsonify({
        "status": SCAN_RESULT.get("status"),          # running | done | error | None
        "finished_at": SCAN_RESULT.get("finished_at"),
        "groups": SCAN_RESULT.get("groups", {}),
        "orphan_s3_files": SCAN_RESULT.get("orphan_s3_files", []),
    })


@app.route("/api/scan/import", methods=["POST"])
def api_scan_import():
    """Import a discovered deployment into the system by creating an S3 config."""
    data = request.json or {}
    uid = data.get("uid")
    if not uid:
        return jsonify({"error": "uid required"}), 400

    groups = SCAN_RESULT.get("groups", {})
    g = groups.get(uid)
    if not g:
        return jsonify({"error": "Unknown deployment UID"}), 404

    res = g["resources"]
    name = g.get("name", f"imported-{uid}")
    region = g.get("region", "us-east-1")
    dep_id = f"{name}-{uid}"

    # Build a deployment record
    jar_filename = res.get("s3_jar_name", res.get("_jar_local_name", "unknown.jar"))
    now = datetime.utcnow().isoformat()
    dep = {
        "id": dep_id,
        "name": name,
        "service_name": _safe_service_name(name),
        "owner": current_user() or "root",
        "jar_filename": jar_filename,
        "created_at": now,
        "status": "deployed" if g["health"] == "healthy" else "broken",
        "region": region,
        "desired_instances": res.get("asg_desired", 1),
        "health_check_path": "/status",
        "version": 1,
        "version_deployed_at": now,
        "history": [],
        "steps": {},
        "resources": {k: v for k, v in res.items() if not k.startswith("_")},
        "_version": 0,
    }

    DEPLOYMENTS[dep_id] = dep
    _save_deployment_to_s3(dep)
    _sync_gateway_config()  # imported service becomes routable

    return jsonify({"status": "imported", "dep_id": dep_id, "name": name})


@app.route("/api/scan/cleanup-s3", methods=["POST"])
def api_scan_cleanup_s3():
    """Remove orphaned S3 config files that have no matching AWS resources."""
    orphans = SCAN_RESULT.get("orphan_s3_files", [])
    removed = []
    errors = []
    for o in orphans:
        try:
            _s3().delete_object(Bucket=CONFIG["s3_bucket"], Key=o["s3_key"])
            DEPLOYMENTS.pop(o["dep_id"], None)
            removed.append(o["dep_id"])
        except Exception as e:
            errors.append(f"{o['dep_id']}: {e}")
    return jsonify({"removed": removed, "errors": errors})


@app.route("/api/scan/teardown", methods=["POST"])
def api_scan_teardown():
    """Import a scanned deployment and immediately start teardown."""
    data = request.json or {}
    uid = data.get("uid")
    if not uid:
        return jsonify({"error": "uid required"}), 400

    groups = SCAN_RESULT.get("groups", {})
    g = groups.get(uid)
    if not g:
        return jsonify({"error": "Unknown deployment UID"}), 404

    res = g["resources"]
    name = g.get("name", f"imported-{uid}")
    region = g.get("region", "us-east-1")
    dep_id = f"{name}-{uid}"

    # Build a deployment record with whatever resources we found
    jar_filename = res.get("s3_jar_name", res.get("_jar_local_name", "unknown.jar"))
    dep = {
        "id": dep_id,
        "name": name,
        "jar_filename": jar_filename,
        "created_at": datetime.utcnow().isoformat(),
        "status": "tearing_down",
        "region": region,
        "desired_instances": 0,
        "steps": {},
        "resources": {k: v for k, v in res.items() if not k.startswith("_")},
        "teardown_steps": {},
        "_version": 0,
        "_td_version": 0,
    }

    # Initialize teardown steps
    for key in TEARDOWN_STEP_KEYS:
        dep["teardown_steps"][key] = {"status": "pending", "detail": "", "label": TEARDOWN_STEP_LABELS[key]}

    DEPLOYMENTS[dep_id] = dep
    _save_deployment_to_s3(dep)

    # Start teardown in background
    t = threading.Thread(target=_run_teardown, args=(dep_id,), daemon=True)
    t.start()

    return jsonify({"status": "started", "dep_id": dep_id})


# ---------------------------------------------------------------------------
# Load existing deployments from S3 on startup & resume interrupted work
# ---------------------------------------------------------------------------
def _resume_interrupted():
    """Resume any deployments/teardowns that were in-flight when the app stopped.

    Uses local pipeline state files for reliable tracking — if a pipeline file
    exists the corresponding pipeline was interrupted mid-flight.
    """
    # Load pipeline files from disk
    pipelines = _load_pipelines()

    for dep_id, dep in list(DEPLOYMENTS.items()):
        status = dep.get("status")

        if status == "deploying":
            pipe = pipelines.get(f"{dep_id}-deploy")
            completed = set(pipe["completed_steps"]) if pipe else set()
            print(f"  ↻ Resuming rollback for interrupted deployment: {dep_id}"
                  f" (completed {len(completed)}/{len(STEP_KEYS)} steps)")
            def _rollback(d_id=dep_id, d=dep):
                try:
                    _teardown_resources_simple(d)
                except Exception:
                    pass
                d["status"] = "failed"
                _delete_deployment_from_s3(d_id)
                _delete_pipeline(d_id, "deploy")
                DEPLOYMENTS.pop(d_id, None)
            t = threading.Thread(target=_rollback, daemon=True)
            t.start()

        elif status == "updating":
            pipe = pipelines.get(f"{dep_id}-update")
            if pipe:
                print(f"  ↻ Reverting interrupted update for: {dep_id}"
                      f" (completed {len(pipe['completed_steps'])}/{len(UPDATE_STEP_KEYS)} steps)")
            else:
                print(f"  ↻ Reverting interrupted update for: {dep_id}")
            dep["status"] = "deployed"
            _save_deployment_to_s3(dep)
            _delete_pipeline(dep_id, "update")

        elif status == "tearing_down":
            pipe = pipelines.get(f"{dep_id}-teardown")
            completed = set(pipe["completed_steps"]) if pipe else set()
            print(f"  ↻ Resuming teardown for: {dep_id}"
                  f" (completed {len(completed)}/{len(TEARDOWN_STEP_KEYS)} steps)")
            td_steps = dep.get("teardown_steps", {})
            # Reset any step that was "running" (interrupted) back to pending
            for key in TEARDOWN_STEP_KEYS:
                step = td_steps.get(key, {})
                if step.get("status") == "running":
                    td_steps[key] = {"status": "pending", "detail": "Resuming after restart",
                                     "label": TEARDOWN_STEP_LABELS[key]}
            # Make sure all steps exist
            for key in TEARDOWN_STEP_KEYS:
                if key not in td_steps:
                    td_steps[key] = {"status": "pending", "detail": "",
                                     "label": TEARDOWN_STEP_LABELS[key]}
            dep["teardown_steps"] = td_steps
            dep["_td_version"] = dep.get("_td_version", 0) + 1

            t = threading.Thread(target=_run_teardown, args=(dep_id,), daemon=True)
            t.start()

    # Clean up stale pipeline files (no matching deployment in memory)
    for key, pipe in pipelines.items():
        d_id = pipe["dep_id"]
        p_type = pipe["type"]
        if d_id not in DEPLOYMENTS:
            print(f"  🗑 Cleaning stale pipeline file: {d_id}-{p_type}")
            _delete_pipeline(d_id, p_type)


def bootstrap():
    """One-time startup: ensure DynamoDB tables, seed root, load deployments, resume."""
    print("Ensuring DynamoDB tables...")
    _ensure_ddb_tables()
    _seed_root_user()
    print("Loading deployments from DynamoDB...")
    _load_deployments_from_s3()
    print(f"Loaded {len(DEPLOYMENTS)} deployment(s).")
    # Reconcile the gateways' routing table with the loaded deployment set.
    _sync_gateway_config()
    _resume_interrupted()


# Run bootstrap when started directly or under gunicorn (APPMESH_BOOTSTRAP=1),
# but NOT when imported as a library (e.g. by initialize_infrastructure.py).
if __name__ == "__main__" or os.environ.get("APPMESH_BOOTSTRAP") == "1":
    bootstrap()

if __name__ == "__main__":
    debug_enabled = str(CONFIG.get("flask_debug", os.environ.get("FLASK_DEBUG", "0"))).lower() in ("1", "true", "yes")
    app.run(debug=debug_enabled, use_reloader=False, port=8081, threaded=True)
