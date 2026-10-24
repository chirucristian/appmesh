"""Local experimental evaluation of the AppMesh gateway (no AWS provisioning).

Two demo backends (local-a:8082, local-b:8086) + the custom gateway (:8080),
with routes served from S3 (route-sync, the real production flow). Measures:
  1. Routing overhead   - latency through the gateway vs. direct to a backend.
  2. Induced failover   - kill the primary backend, observe zero-loss retry to
                          the secondary and the circuit breaker opening.
  3. Recovery           - restart the primary, observe traffic returning to it.
  4. Config propagation - change the S3 route config, time until the gateway
                          reflects it (hot reload, no restart).

The gateway forwards to the demo home page ('/'); the page embeds the instance
'version' (local-a / local-b), which identifies the serving backend.
Results -> results.json.
"""
from __future__ import annotations
import subprocess, time, json, statistics, re, os
from pathlib import Path
import requests

from common import ROOT, load_config, java_executable

AMB  = ROOT / "apps" / "ambulanta" / "target" / "ambulance-service.jar"
GW   = ROOT / "lb" / "target" / "gateway-lb-0.0.1-SNAPSHOT.jar"
EVAL = ROOT / "_local_eval"
GW_CONFIG = EVAL / "gw-local.yml"

DIRECT_A = "http://localhost:8082/"
HEALTH_A = "http://localhost:8082/status"
HEALTH_B = "http://localhost:8086/status"
GW_HOME  = "http://localhost:8080/ambulance-service/"
GW_STATUS = "http://localhost:8080/gateway/status"
GW_HEALTH = "http://localhost:8080/actuator/health"

# ---- AWS creds + S3 (read from config.properties; never printed) ----
CFG = load_config()
JAVA = java_executable(CFG)
import boto3
S3 = boto3.client("s3", region_name=CFG.get("aws_region", "us-east-1"),
                  aws_access_key_id=CFG["aws_access_key_id"],
                  aws_secret_access_key=CFG["aws_secret_access_key"])
BUCKET, S3KEY = CFG["s3_bucket"], "conf/lb/eval/application.yml"

def s3_put(path):
    S3.put_object(Bucket=BUCKET, Key=S3KEY, Body=Path(path).read_bytes(), ContentType="text/yaml")

GW_ENV = dict(os.environ, AWS_ACCESS_KEY_ID=CFG["aws_access_key_id"],
              AWS_SECRET_ACCESS_KEY=CFG["aws_secret_access_key"])

procs: dict[str, tuple] = {}
def start(name, args, logfile, env=None):
    f = open(EVAL / logfile, "w", encoding="utf-8")
    p = subprocess.Popen([JAVA] + args, stdout=f, stderr=subprocess.STDOUT, env=env)
    procs[name] = (p, f); print(f"  started {name} (pid {p.pid})"); return p

def stop(name):
    if name in procs:
        p, f = procs.pop(name)
        try: p.terminate(); p.wait(timeout=10)
        except Exception:
            try: p.kill()
            except Exception: pass
        try: f.close()
        except Exception: pass
        print(f"  stopped {name}")

def backend_of(text):
    m = re.search(r"local-[ab]", text or "")
    return m.group(0) if m else None

def wait_health(url, timeout=90, want=None):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = requests.get(url, timeout=3)
            if r.status_code == 200 and (want is None or want in r.text):
                return True
        except Exception: pass
        time.sleep(1)
    return False

def pct(s, q):
    s = sorted(s); k = (len(s) - 1) * q; f = int(k); c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)

def summarize(lat):
    return {"n": len(lat), "mean_ms": round(statistics.mean(lat), 2),
            "median_ms": round(statistics.median(lat), 2),
            "p95_ms": round(pct(lat, 0.95), 2), "p99_ms": round(pct(lat, 0.99), 2),
            "min_ms": round(min(lat), 2), "max_ms": round(max(lat), 2)}

def bench(url, n=200):
    lat, codes = [], {}
    for _ in range(10):
        try: requests.get(url, timeout=5)
        except Exception: pass
    for _ in range(n):
        t = time.perf_counter()
        try:
            r = requests.get(url, timeout=5)
            lat.append((time.perf_counter() - t) * 1000.0)
            codes[r.status_code] = codes.get(r.status_code, 0) + 1
        except Exception:
            codes["err"] = codes.get("err", 0) + 1
    return lat, codes

def cb_regions():
    try:
        d = requests.get(GW_STATUS, timeout=3).json()
        return {r["name"]: {"cb": r["circuitBreaker"], "healthy": r["healthy"]}
                for r in d["ambulance-service"]["regions"]}
    except Exception as e:
        return {"error": str(e)}

results = {}
try:
    print("[1/6] S3 config + backends...")
    s3_put(EVAL / "conf_eval.yml")
    start("demoA", ["-jar", str(AMB), "--server.port=8082", "--app.version=local-a"], "demoA.log")
    start("demoB", ["-jar", str(AMB), "--server.port=8086", "--app.version=local-b"], "demoB.log")
    assert wait_health(HEALTH_A, 90, "local-a"), "demo A unhealthy"
    assert wait_health(HEALTH_B, 90, "local-b"), "demo B unhealthy"
    print("  backends healthy")

    print("[2/6] Gateway (route-sync from S3)...")
    start("gw", ["-jar", str(GW), "--spring.config.location=file:" + str(GW_CONFIG).replace("\\", "/")],
          "gw.log", env=GW_ENV)
    assert wait_health(GW_HEALTH, 120), "gateway health down"
    assert wait_health(GW_HOME, 60, "local-"), "gateway route not serving"
    time.sleep(20)  # let active health-checks settle
    results["start_state"] = {"primary": backend_of(requests.get(GW_HOME, timeout=5).text),
                              "cb": cb_regions()}
    print("  serving, primary =", results["start_state"]["primary"])

    print("[3/6] Routing overhead...")
    ld, cd = bench(DIRECT_A, 200)
    lg, cg = bench(GW_HOME, 200)
    results["overhead"] = {
        "direct": {"summary": summarize(ld), "codes": cd},
        "gateway": {"summary": summarize(lg), "codes": cg},
        "overhead_mean_ms": round(statistics.mean(lg) - statistics.mean(ld), 2),
        "overhead_median_ms": round(statistics.median(lg) - statistics.median(ld), 2)}
    print("  mean overhead:", results["overhead"]["overhead_mean_ms"], "ms")

    print("[4/6] Induced failover (kill local-a)...")
    kill_t = time.time(); stop("demoA")
    events, snaps, last = [], [], 0
    end = time.time() + 40
    while time.time() < end:
        rel = round(time.time() - kill_t, 3); t = time.perf_counter()
        try:
            r = requests.get(GW_HOME, timeout=6)
            events.append({"t": rel, "code": r.status_code,
                           "backend": backend_of(r.text),
                           "lat_ms": round((time.perf_counter() - t) * 1000, 1)})
        except Exception:
            events.append({"t": rel, "code": "err", "backend": None, "lat_ms": None})
        if time.time() - last > 5:
            snaps.append({"t": rel, "cb": cb_regions()}); last = time.time()
        time.sleep(0.25)
    first_b = next((e for e in events if e["backend"] == "local-b"), None)
    nerr = sum(1 for e in events if e["code"] == "err" or (isinstance(e["code"], int) and e["code"] >= 500))
    results["failover"] = {"requests": len(events), "failed_requests": nerr,
                           "first_secondary_t": first_b["t"] if first_b else None,
                           "events_head": events[:24], "cb_snapshots": snaps}
    print(f"  {len(events)} reqs, {nerr} failed, first local-b @ {first_b['t'] if first_b else 'N/A'}s")

    print("[5/6] Recovery (restart local-a)...")
    rt = time.time()
    start("demoA", ["-jar", str(AMB), "--server.port=8082", "--app.version=local-a"], "demoA2.log")
    wait_health(HEALTH_A, 90, "local-a")
    healthy_t = round(time.time() - rt, 1)
    rev, rcb, last, back = [], [], 0, None
    end = time.time() + 80
    while time.time() < end:
        rel = round(time.time() - rt, 3)
        try:
            r = requests.get(GW_HOME, timeout=6); b = backend_of(r.text)
            rev.append({"t": rel, "backend": b})
            if b == "local-a" and back is None and rel > healthy_t:
                back = rel
        except Exception:
            rev.append({"t": rel, "backend": "err"})
        if time.time() - last > 5:
            rcb.append({"t": rel, "cb": cb_regions()}); last = time.time()
        time.sleep(0.5)
    results["recovery"] = {"backend_healthy_after_s": healthy_t,
                           "traffic_back_to_primary_t": back,
                           "cb_snapshots": rcb}
    print(f"  backend healthy @ {healthy_t}s, traffic back to local-a @ {back}s")

    print("[6/6] Config hot-reload propagation...")
    # switch S3 config to a single region; time until the gateway reflects it
    single = EVAL / "conf_eval_1region.yml"
    single.write_text(
        "gateway:\n  services:\n    ambulance-service:\n      paths:\n        - /ambulance-service/**\n"
        "      health-check-path: /status\n      regions:\n        - name: local-a\n          url: http://localhost:8082\n",
        encoding="utf-8")
    before = len(cb_regions())
    swap_t = time.time(); s3_put(single)
    prop = None
    while time.time() - swap_t < 30:
        regs = cb_regions()
        if isinstance(regs, dict) and "error" not in regs and len(regs) == 1:
            prop = round(time.time() - swap_t, 2); break
        time.sleep(0.5)
    results["config_propagation"] = {"poll_interval_ms": 10000,
                                     "regions_before": before, "regions_after": 1,
                                     "propagation_s": prop}
    print(f"  propagation: {prop}s (poll interval 10s)")

    (EVAL / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print("\nWROTE results.json")
finally:
    print("\nTearing down...")
    for n in list(procs): stop(n)
    try: S3.delete_object(Bucket=BUCKET, Key=S3KEY); print("  deleted S3 eval config")
    except Exception as e: print("  S3 cleanup:", e)
print("DONE")
