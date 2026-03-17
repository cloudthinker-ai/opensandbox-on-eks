#!/bin/bash
# Verify an OpenSandbox EKS deployment by spinning up a temporary pod
# inside the cluster and running tests against the server via in-cluster DNS.
# No port-forwarding needed. The pod auto-deletes when done.
#
# Usage:
#   API_KEY="your-api-key" ./scripts/verify-eks-deployment.sh
#
# Environment variables:
#   API_KEY        - OpenSandbox API key (required)
#   NAMESPACE      - Server namespace (default: opensandbox)
#   TIMEOUT        - Seconds to wait for state transitions (default: 120)
#   SKIP_LIFECYCLE - Set to "1" to only test health/auth, skip sandbox lifecycle

set -euo pipefail

API_KEY="${API_KEY:-}"
NAMESPACE="${NAMESPACE:-opensandbox}"
TIMEOUT="${TIMEOUT:-120}"
SKIP_LIFECYCLE="${SKIP_LIFECYCLE:-0}"
SERVER_URL="http://opensandbox-server.${NAMESPACE}:8080"

if [[ -z "$API_KEY" ]]; then
  echo "Warning: API_KEY not set. Auth and lifecycle tests will be skipped/fail." >&2
fi

echo "Running OpenSandbox deployment verification in-cluster..."
echo "  Namespace:  $NAMESPACE"
echo "  Server URL: $SERVER_URL"
echo "  Timeout:    ${TIMEOUT}s"
echo ""

kubectl run opensandbox-verify \
  --image=python:3.11-slim \
  --namespace="$NAMESPACE" \
  --restart=Never \
  --rm \
  -i \
  --env="API_KEY=${API_KEY}" \
  --env="SERVER_URL=${SERVER_URL}" \
  --env="TIMEOUT=${TIMEOUT}" \
  --env="SKIP_LIFECYCLE=${SKIP_LIFECYCLE}" \
  --override-type=strategic \
  --overrides='{"spec":{"terminationGracePeriodSeconds":5}}' \
  -- python3 -u - <<'TEST_SCRIPT'
import os, sys, json, time
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

API_KEY = os.environ.get("API_KEY", "")
SERVER_URL = os.environ.get("SERVER_URL", "http://opensandbox-server.opensandbox:8080")
TIMEOUT = int(os.environ.get("TIMEOUT", "120"))
SKIP_LIFECYCLE = os.environ.get("SKIP_LIFECYCLE", "0")

PASS = 0
FAIL = 0
WARN = 0
SANDBOX_ID = ""

def step(msg):
    print(f"\n==== {msg} ====")

def passed(msg):
    global PASS; PASS += 1; print(f"  PASS: {msg}")

def fail(msg):
    global FAIL; FAIL += 1; print(f"  FAIL: {msg}", file=sys.stderr)

def warn(msg):
    global WARN; WARN += 1; print(f"  WARN: {msg}", file=sys.stderr)

def info(msg):
    print(f"  {msg}")

def http_request(url, method="GET", data=None, headers=None, timeout=10):
    """Returns (status_code, body_dict_or_str). Never raises."""
    hdrs = headers or {}
    if API_KEY:
        hdrs["OPEN-SANDBOX-API-KEY"] = API_KEY
    body_bytes = None
    if data is not None:
        body_bytes = json.dumps(data).encode()
        hdrs["Content-Type"] = "application/json"
    req = Request(url, data=body_bytes, headers=hdrs, method=method)
    try:
        resp = urlopen(req, timeout=timeout)
        raw = resp.read().decode()
        try:
            return resp.status, json.loads(raw)
        except json.JSONDecodeError:
            return resp.status, raw
    except HTTPError as e:
        raw = e.read().decode() if e.fp else ""
        try:
            return e.code, json.loads(raw)
        except (json.JSONDecodeError, Exception):
            return e.code, raw
    except Exception as e:
        return 0, str(e)

def http_no_auth(url, timeout=10):
    """Request WITHOUT API key."""
    req = Request(url)
    try:
        resp = urlopen(req, timeout=timeout)
        return resp.status, resp.read().decode()
    except HTTPError as e:
        return e.code, e.read().decode() if e.fp else ""
    except Exception:
        return 0, ""

def wait_for_state(sandbox_id, target, timeout=None):
    """Poll until sandbox reaches target state. Returns (ok, last_body)."""
    deadline = time.time() + (timeout or TIMEOUT)
    while True:
        code, body = http_request(f"{SERVER_URL}/v1/sandboxes/{sandbox_id}")
        state = ""
        if isinstance(body, dict):
            state = body.get("status", {}).get("state", "")
        if state == target:
            return True, body
        if state in ("Failed", "Terminated"):
            return False, body
        if time.time() >= deadline:
            return False, body
        time.sleep(3)

def cleanup():
    if SANDBOX_ID:
        info(f"Cleaning up sandbox {SANDBOX_ID}...")
        http_request(f"{SERVER_URL}/v1/sandboxes/{SANDBOX_ID}", method="DELETE")

# =========================================================================
# 1. Health
# =========================================================================
step("Server Health")

code, body = http_no_auth(f"{SERVER_URL}/health")
if code == 200:
    try:
        data = json.loads(body) if isinstance(body, str) else body
        if data.get("status") == "healthy":
            passed('GET /health -> healthy')
        else:
            fail(f"GET /health unexpected: {body}")
    except Exception:
        fail(f"GET /health unexpected: {body}")
else:
    fail(f"GET /health unreachable (status {code})")

# =========================================================================
# 2. Authentication
# =========================================================================
step("API Authentication")

unauth_code, _ = http_no_auth(f"{SERVER_URL}/v1/sandboxes")

if unauth_code == 401:
    passed("Unauthenticated request rejected (401)")
elif unauth_code == 200:
    warn("No authentication configured")
else:
    fail(f"Unexpected status {unauth_code} without auth")

if API_KEY:
    auth_code, _ = http_request(f"{SERVER_URL}/v1/sandboxes")
    if auth_code == 200:
        passed("Authenticated request OK (200)")
    else:
        fail(f"Authenticated request returned {auth_code}")
else:
    warn("API_KEY not set — skipping auth test")

# =========================================================================
# 3. Full Lifecycle: create -> run -> list -> renew -> pause -> resume -> delete
# =========================================================================
try:
    if SKIP_LIFECYCLE == "1":
        step("Sandbox Lifecycle (SKIPPED)")
    elif not API_KEY and unauth_code == 401:
        step("Sandbox Lifecycle")
        fail("API requires auth but API_KEY is not set")
    else:
        # --- Create ---
        step("Create Sandbox")
        info("Creating test sandbox...")
        code, body = http_request(f"{SERVER_URL}/v1/sandboxes", method="POST", data={
            "image": {"uri": "ubuntu:24.04"},
            "entrypoint": ["tail", "-f", "/dev/null"],
            "timeout": 600,
            "metadata": {"opensandbox-verify": "true"},
            "resourceLimits": {"cpu": "500m", "memory": "512Mi"},
        }, timeout=120)

        if code == 202 and isinstance(body, dict) and body.get("id"):
            SANDBOX_ID = body["id"]
            passed(f"Created: {SANDBOX_ID}")
        else:
            fail(f"Create returned HTTP {code}")
            if isinstance(body, dict):
                info(json.dumps(body))
            else:
                info(str(body))

        # --- Wait for Running ---
        if SANDBOX_ID:
            info(f"Waiting for Running (timeout: {TIMEOUT}s)...")
            ok, resp = wait_for_state(SANDBOX_ID, "Running")
            if ok:
                passed("Sandbox is Running")
            else:
                state = resp.get("status", {}).get("state", "unknown") if isinstance(resp, dict) else "unknown"
                reason = resp.get("status", {}).get("reason", "") if isinstance(resp, dict) else ""
                fail(f"Not Running (state: {state}, reason: {reason})")

        # --- List ---
        if SANDBOX_ID:
            step("List Sandboxes")
            code, body = http_request(f"{SERVER_URL}/v1/sandboxes?metadata=opensandbox-verify%3Dtrue")
            found = False
            if isinstance(body, dict):
                for item in body.get("items", []):
                    if item.get("id") == SANDBOX_ID:
                        found = True
                        break
            if found:
                passed("Found sandbox in list")
            else:
                fail("Sandbox not found in list")

        # --- Renew expiration ---
        if SANDBOX_ID:
            step("Renew Expiration")
            from datetime import datetime, timedelta, timezone
            new_exp = (datetime.now(timezone.utc) + timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
            code, _ = http_request(
                f"{SERVER_URL}/v1/sandboxes/{SANDBOX_ID}/renew-expiration",
                method="POST", data={"expiresAt": new_exp},
            )
            if code == 200:
                passed("Expiration renewed")
            else:
                fail(f"Renew returned HTTP {code}")

        # --- Pause ---
        if SANDBOX_ID:
            step("Pause Sandbox")
            info("Pausing...")
            code, _ = http_request(f"{SERVER_URL}/v1/sandboxes/{SANDBOX_ID}/pause", method="POST", timeout=60)
            if code == 202:
                passed("Pause accepted (202)")
            else:
                fail(f"Pause returned HTTP {code}")

            info(f"Waiting for Paused (timeout: {TIMEOUT}s)...")
            ok, resp = wait_for_state(SANDBOX_ID, "Paused")
            if ok:
                passed("Sandbox is Paused")
            else:
                state = resp.get("status", {}).get("state", "unknown") if isinstance(resp, dict) else "unknown"
                fail(f"Not Paused (state: {state})")

            # Snapshot is created async after pause. Wait before resuming.
            # Resume success proves the snapshot worked (it restores from it).
            info("Waiting 30s for snapshot to complete in background...")
            time.sleep(30)

        # --- Resume ---
        if SANDBOX_ID:
            step("Resume Sandbox")
            info("Resuming...")
            code, _ = http_request(f"{SERVER_URL}/v1/sandboxes/{SANDBOX_ID}/resume", method="POST")
            if code == 202:
                passed("Resume accepted (202)")
            else:
                fail(f"Resume returned HTTP {code}")

            info(f"Waiting for Running after resume (timeout: {TIMEOUT}s)...")
            ok, resp = wait_for_state(SANDBOX_ID, "Running")
            if ok:
                passed("Sandbox is Running after resume (snapshot restore worked)")
            else:
                state = resp.get("status", {}).get("state", "unknown") if isinstance(resp, dict) else "unknown"
                reason = resp.get("status", {}).get("reason", "") if isinstance(resp, dict) else ""
                fail(f"Not Running after resume (state: {state}, reason: {reason})")

        # --- Delete ---
        if SANDBOX_ID:
            step("Delete Sandbox")
            info("Deleting...")
            code, _ = http_request(f"{SERVER_URL}/v1/sandboxes/{SANDBOX_ID}", method="DELETE")
            if code in (200, 204):
                passed("Sandbox deleted")
                SANDBOX_ID = ""
            elif code == 404:
                passed("Sandbox already gone")
                SANDBOX_ID = ""
            else:
                fail(f"Delete returned HTTP {code}")

except Exception as e:
    fail(f"Unexpected error: {e}")
finally:
    cleanup()

# =========================================================================
# Summary
# =========================================================================
step("Summary")
print()
parts = [f"  {PASS} passed"]
if FAIL > 0:
    parts.append(f"  {FAIL} failed")
if WARN > 0:
    parts.append(f"  {WARN} warnings")
print("".join(parts))
print()
if FAIL > 0:
    print("Some checks failed.")
    sys.exit(1)
else:
    print("Deployment verification passed!")
TEST_SCRIPT
