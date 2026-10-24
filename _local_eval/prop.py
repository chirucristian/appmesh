"""Focused config hot-reload measurement: change the S3 route config so the
service routes to a different backend, and time how long until the running
gateway serves from the new backend (no restart). Detection via served backend."""
from __future__ import annotations
import subprocess, time, re, os, json
from pathlib import Path
import requests, boto3

from common import ROOT, load_config, java_executable

AMB  = ROOT / "apps" / "ambulanta" / "target" / "ambulance-service.jar"
GW   = ROOT / "lb" / "target" / "gateway-lb-0.0.1-SNAPSHOT.jar"
EVAL = ROOT / "_local_eval"
GW_HOME = "http://localhost:8080/ambulance-service/"

CFG = load_config()
JAVA = java_executable(CFG)
S3 = boto3.client("s3", region_name=CFG.get("aws_region", "us-east-1"),
                  aws_access_key_id=CFG["aws_access_key_id"], aws_secret_access_key=CFG["aws_secret_access_key"])
BUCKET, KEY = CFG["s3_bucket"], "conf/lb/eval/application.yml"
ENV = dict(os.environ, AWS_ACCESS_KEY_ID=CFG["aws_access_key_id"], AWS_SECRET_ACCESS_KEY=CFG["aws_secret_access_key"])

TWO = """gateway:
  services:
    ambulance-service:
      paths: [ /ambulance-service/** ]
      health-check-path: /status
      regions:
        - { name: local-a, url: http://localhost:8082 }
        - { name: local-b, url: http://localhost:8086 }
  circuit-breaker: { failure-threshold: 3, success-threshold: 2, half-open-delay-seconds: 30 }
  health-check: { interval-seconds: 15, timeout-seconds: 5 }
  timeout: { connect-timeout-ms: 2000, read-timeout-ms: 5000 }
"""
ONLY_B = """gateway:
  services:
    ambulance-service:
      paths: [ /ambulance-service/** ]
      health-check-path: /status
      regions:
        - { name: local-b, url: http://localhost:8086 }
"""
def put(body): S3.put_object(Bucket=BUCKET, Key=KEY, Body=body.encode(), ContentType="text/yaml")
def backend():
    m = re.search(r"local-[ab]", requests.get(GW_HOME, timeout=6).text); return m.group(0) if m else None

procs = []
def start(args, log):
    f = open(EVAL/log, "w"); p = subprocess.Popen([JAVA]+args, stdout=f, stderr=subprocess.STDOUT, env=ENV); procs.append((p,f)); return p

try:
    put(TWO)
    start(["-jar", str(AMB), "--server.port=8082", "--app.version=local-a"], "pA.log")
    start(["-jar", str(AMB), "--server.port=8086", "--app.version=local-b"], "pB.log")
    time.sleep(8)
    start(["-jar", str(GW), "--spring.config.location=file:" + (EVAL/'gw-local.yml').as_posix()], "pGW.log")
    # wait until serving from local-a (primary)
    t0 = time.time()
    while time.time()-t0 < 120:
        try:
            if backend() == "local-a": break
        except Exception: pass
        time.sleep(1)
    print("serving primary:", backend())
    time.sleep(12)  # settle
    print("swapping S3 config -> only local-b ...")
    swap = time.time(); put(ONLY_B)
    prop = None
    while time.time()-swap < 30:
        try:
            if backend() == "local-b": prop = round(time.time()-swap, 2); break
        except Exception: pass
        time.sleep(0.25)
    print("PROPAGATION_SECONDS:", prop)
    Path(EVAL/"prop_result.json").write_text(json.dumps({"poll_interval_ms":10000,"propagation_s":prop}), encoding="utf-8")
finally:
    for p,f in procs:
        try: p.terminate(); p.wait(timeout=10)
        except Exception:
            try: p.kill()
            except Exception: pass
        f.close()
    try: S3.delete_object(Bucket=BUCKET, Key=KEY); print("deleted S3 eval config")
    except Exception as e: print("cleanup:", e)
print("DONE")
