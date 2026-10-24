# AppMesh

A self-hosted, multi-region deployment platform for JAR-packaged services, built
as a master's dissertation project (University Politehnica of Bucharest). It
provisions real AWS infrastructure from a web console: upload a Spring Boot JAR,
pick a region, and the platform creates the launch template, auto-scaling group,
target group and host-header rule on a shared regional ALB, then publishes a DNS
record through Cloudflare.

Traffic enters through a custom Spring Cloud Gateway that reads its routing table
from S3 at runtime, health-checks every region, and fails over between them with
a circuit breaker — without a restart.

> The dissertation itself (in Romanian) is in [`dizertatie/`](dizertatie/), including
> the compiled PDF and the figure sources.

## Repository layout

| Path | What it is |
| --- | --- |
| `admin_console/` | Flask control plane — the web console. Provisioning, scaling, teardown, AWS/Cloudflare drift scanning. |
| `lb/` | The gateway load balancer (Spring Cloud Gateway, Java 17). Dynamic routes from S3, per-region health checks, circuit breaker. |
| `apps/ambulanta/`, `apps/politia/` | Demo Spring Boot services (Java 21) standing in for two e-government services. |
| `apps/landing/` | The public landing page listing every deployed service. |
| `_local_eval/` | Laptop-only evaluation harness: routing overhead, induced failover, recovery, config propagation. Produces `results.json`. |
| `appmesh/design_handoff_console_v3/` | Design handoff for the console UI (static mock-ups, not wired to the backend). |
| `dizertatie/` | The dissertation (LaTeX + PDF + figures). |

## Architecture in one paragraph

The console is the control plane: it writes deployment metadata to DynamoDB,
uploads JARs to S3, and drives the AWS APIs directly via boto3 — there is no
Terraform or CloudFormation. Every region that is "onboarded" gets one shared
ALB, one gateway instance and one Cloudflare origin pool. After any change to
the service portfolio, the console regenerates a per-region
`conf/lb/<region>/application.yml` in S3; each gateway polls its own object
every 30 s and rebuilds its routes in place. Cloudflare's load balancer steers
users to the nearest healthy region; inside a region, the gateway steers between
regions when its own health checks say the local backend is down.

## Prerequisites

- Python 3.11+ and Java 21 (Java 17 is enough for the gateway alone)
- Maven 3.9+
- An AWS account with programmatic access (EC2, ELBv2, Auto Scaling, IAM, S3, DynamoDB)
- An S3 bucket (JAR storage + gateway route configs) — keep Block Public Access **on**
- Optional: a Cloudflare zone and API token for DNS and load balancing.
  Set `skip_cloudflare = true` to run without it.

## Configuration

Every credential the platform needs lives in one git-ignored file. Nothing in
this repository contains a real key, token or password.

```bash
cp admin_console/config.properties.example admin_console/config.properties
```

Then fill it in — [`config.properties.example`](admin_console/config.properties.example)
documents all 26 keys. The minimum to get started:

- `aws_access_key_id` / `aws_secret_access_key` — or leave both blank to use the
  default credential chain (instance role, `AWS_*` env vars, `~/.aws/credentials`)
- `s3_bucket`
- `flask_secret_key` — `python -c "import secrets; print(secrets.token_urlsafe(48))"`
- `root_username` / `root_password` — seeded into DynamoDB on first start
- `skip_cloudflare = true` if you have no Cloudflare zone

The DynamoDB tables (`appmesh-users`, `appmesh-deployments`) are created
automatically on first run.

## Running the console

```bash
cd admin_console
python -m venv .venv && .venv/Scripts/activate   # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python _run_console.py
```

The console listens on <http://127.0.0.1:8081>. Log in with the root account
from `config.properties`, onboard a region, then deploy a JAR.

To build the demo services and the gateway:

```bash
mvn -q -f apps/ambulanta/pom.xml package
mvn -q -f apps/politia/pom.xml package
mvn -q -f lb/pom.xml package
```

## Local evaluation (no AWS provisioning)

`_local_eval/` reproduces the measurements reported in the dissertation on a
single machine: two demo backends plus the gateway, with routes served from the
real S3 route-sync path. It needs `s3_bucket` and AWS credentials in
`config.properties`, and writes to `conf/lb/eval/application.yml` in that bucket.

```bash
python _local_eval/eval.py     # overhead, failover, recovery  -> results.json
python _local_eval/prop.py     # config propagation time       -> prop_result.json
```

Set `java_executable` in `config.properties` if `java` is not on your `PATH`.

## Security notes

This is a dissertation prototype, not production software. Known, deliberate
limitations:

- Credentials sit in a plaintext properties file rather than a secret manager.
- Security-group ingress for deployed workloads is permissive.
- The gateway↔backend hop is plain HTTP; there is no mTLS.
- Deployment state is partly process-local, with persistence delegated to
  DynamoDB and S3.

If you deploy this, treat the AWS account as disposable and rotate the keys when
you are done.

## Licence

No licence is granted; the code is published as supporting material for the
dissertation.
