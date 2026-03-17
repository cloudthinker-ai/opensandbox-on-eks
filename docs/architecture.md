# Opensandbox Architecture

This document describes the OpenSandbox architecture for AI coding agents, including the Kubernetes runtime internals.

## Overview

OpenSandbox provisions isolated development environments for AI agents. Each agent gets a Kubernetes pod running a sandbox image with pre-installed tools. Filesystem changes persist across pod restarts via OverlayFS on a PVC.

```
SDK Clients (Python / TypeScript / Java)
          |
          | HTTP / SSE
          v
+--------------------+
| Lifecycle Server   |
| (FastAPI)          |
+--------+-----------+
         |
         v
+--------------------+       +------------------+
| Kubernetes Runtime |------>| Controller (Go)  |
| (BatchSandbox CR)  |       | Reconcile loop   |
+--------------------+       +--------+---------+
                                      |
                              Creates / manages
                                      |
                                      v
                              +---------------+
                              | Sandbox Pod   |
                              | (per agent)   |
                              +---------------+
```

**Request flow:**

1. SDK sends `POST /sandboxes` with image, resources, and entrypoint
2. Server creates a `BatchSandbox` CR in Kubernetes
3. Controller reconciles: creates Pod + PVC
4. Init container copies `execd` binary and `bootstrap.sh` into the pod
5. `bootstrap.sh` sets up OverlayFS (if enabled), starts execd, drops to user entrypoint
6. SDK communicates with execd inside the pod for code execution, file ops, and commands

## System Components

```
                          SDK Clients
                   (Python / TypeScript / Java)
                             |
                             | HTTP / SSE
                             v
               +--------------------------+
               |     Lifecycle Server     |
               |       (FastAPI)          |
               +-----------+--------------+
                           |
             +-------------+-------------+
             |                           |
             v                           v
  +-------------------+      +-------------------+
  | Docker Runtime    |      | Kubernetes Runtime|
  | (single-node dev) |      | (production)      |
  +-------------------+      +--------+----------+
                                      |
                         +------------+------------+
                         |                        |
                         v                        v
                  BatchSandbox              VolumeSnapshot
                      CR                    (CSI snapshot
                  (per sandbox)             for durability)
                         |
                         v
                  +-------------+
                  | Controller  |
                  | (Go)        |
                  +------+------+
                         |
                  Reconcile loop
                         |
                         v
                  Sandbox Pods + PVCs
```

## Server Internal Layers

```
+-------------------------------------------------------------------+
|                        FastAPI Server                              |
|                                                                   |
|  +---------------------+    +-------------------------------+     |
|  |    API Layer         |    |    Configuration              |     |
|  |  (src/api/)          |    |  (src/config.py)              |     |
|  |                      |    |                               |     |
|  |  lifecycle.py        |    |  .sandbox.toml                |     |
|  |   POST /sandboxes    |    |   [server]  [runtime]         |     |
|  |   GET  /sandboxes    |    |   [kubernetes]  [ingress]     |     |
|  |   DELETE /{id}       |    |   [storage]  [egress]         |     |
|  |   POST  /{id}/pause  |    +-------------------------------+     |
|  |   POST  /{id}/resume |                                         |
|  |   GET   /endpoints   |                                         |
|  |   ANY   /proxy/*     |                                         |
|  +----------+-----------+                                         |
|             |                                                     |
|             v                                                     |
|  +---------------------+                                         |
|  |   Service Layer      |                                         |
|  |                      |                                         |
|  |  KubernetesSandbox   |                                         |
|  |  Service             |                                         |
|  +----------+-----------+                                         |
|             |                                                     |
|             v                                                     |
|  +-----------------------------------------------+               |
|  |   Kubernetes Subsystem (src/services/k8s/)     |               |
|  |                                                |               |
|  |  WorkloadProvider (abstract)                   |               |
|  |       |                                        |               |
|  |       +-- BatchSandboxProvider (main impl)     |               |
|  |                                                |               |
|  |  SnapshotManager .... VolumeSnapshot CRUD      |               |
|  |  WorkloadInformer ... Watch-based cache        |               |
|  |  TemplateManager .... YAML template merge      |               |
|  |  EgressHelper ....... Network policy injection |               |
|  |  K8sClient .......... API client wrapper       |               |
|  +------------------------------------------------+               |
+-------------------------------------------------------------------+
```

## Architecture Diagram

```
+===========================================================================+
|                           Kubernetes Cluster                              |
|                                                                           |
|  +----------------------------+    +----------------------------+         |
|  | opensandbox namespace      |    | opensandbox-system         |         |
|  |                            |    |                            |         |
|  |  +----------------------+  |    |  +----------------------+  |         |
|  |  | Server (FastAPI)     |  |    |  | Controller (Go)      |  |         |
|  |  | - Lifecycle API      |  |    |  | - BatchSandbox CRD   |  |         |
|  |  | - Proxy to execd     |  |    |  | - Scale expectations |  |         |
|  |  | - Snapshot manager   |  |    |  |                      |  |         |
|  |  +----------------------+  |    |  +----------------------+  |         |
|  +----------------------------+    +----------------------------+         |
|                                                                           |
|  +====================================================================+  |
|  |                        Sandbox Pod                                  |  |
|  |  shareProcessNamespace: true                                        |  |
|  |                                                                     |  |
|  |  INIT:  execd-installer (copies execd + bootstrap.sh to emptyDir)   |  |
|  |                                                                     |  |
|  |  +---------------------------------------------------------------+ |  |
|  |  | sandbox container                                              | |  |
|  |  |                                                                | |  |
|  |  |  bootstrap.sh (PID 1, root)                                    | |  |
|  |  |    |                                                           | |  |
|  |  |    +-- Phase 1: OverlayFS setup (if OVERLAY_PERSIST=1)         | |  |
|  |  |    |   mount overlay -> pivot_root -> drop caps                | |  |
|  |  |    |                                                           | |  |
|  |  |    +-- Phase 2: Start execd daemon (background, restart loop)  | |  |
|  |  |    |   execd :44772 — code exec, file ops, commands, metrics   | |  |
|  |  |    |                                                           | |  |
|  |  |    +-- Phase 3: Drop to SANDBOX_USER, exec user entrypoint    | |  |
|  |  |                                                                | |  |
|  |  |  Available inside the sandbox:                                 | |  |
|  |  |    - Cloud CLIs (aws, gcloud, az, gh, kubectl)                 | |  |
|  |  |    - Runtimes (Python 3.12, Node.js 20, Bun, Java 21)         | |  |
|  |  +---------------------------------------------------------------+ |  |
|  |                                                                     |  |
|  |  +---------------------------+  (optional)                          |  |
|  |  | egress sidecar            |                                      |  |
|  |  | DNS proxy + nftables      |                                      |  |
|  |  | FQDN-based egress rules   |                                      |  |
|  |  +---------------------------+                                      |  |
|  |                                                                     |  |
|  |  VOLUMES:                                                           |  |
|  |  +----------------------------+  +-------------------------------+  |  |
|  |  | opensandbox-bin (emptyDir) |  | sandbox-data (PVC, 20Gi RWO) |  |  |
|  |  | /opt/opensandbox/bin       |  | /mnt/sandbox-data             |  |  |
|  |  | execd + bootstrap.sh       |  | overlay-upper/ (all writes)  |  |  |
|  |  +----------------------------+  | overlay-work/  (kernel)      |  |  |
|  |                                  | Persists across pause/resume |  |  |
|  |                                  +-------------------------------+  |  |
|  +====================================================================+  |
+===========================================================================+
```

## OverlayFS Persistence

OpenSandbox uses OverlayFS to persist the entire filesystem. All writes (installed packages, config changes, user files) are captured in the PVC-backed upper directory and survive pod restarts.

```
Merged root (/) = Lower (container image, read-only) + Upper (PVC, read-write)

┌──────────────────────────────────────────────┐
│ /mnt/newroot (merged view)                   │
│                                              │
│ Read /usr/bin/python:                        │
│   kernel checks UPPER → not found            │
│   kernel checks LOWER → found → returns it   │
│                                              │
│ Write /tmp/file.txt:                         │
│   kernel writes to UPPER (PVC) directly      │
│                                              │
│ Modify /etc/hosts (exists in LOWER):         │
│   kernel copies LOWER→UPPER (copy-on-write)  │
│   kernel applies write to UPPER copy          │
│   future reads → served from UPPER            │
│                                              │
│ Delete /etc/old.conf:                        │
│   kernel creates whiteout in UPPER            │
│   (.wh.old.conf masks the LOWER file)        │
└──────────────────────────────────────────────┘
```

**Why this works for persistence:** The UPPER directory lives on the PVC. When the pod is killed (pause), the PVC survives. When the pod is recreated (resume), the same PVC is remounted, and OverlayFS sees all previous writes in UPPER.

### Bootstrap Sequence (`components/execd/bootstrap.sh`)

`bootstrap.sh` is the PID 1 entrypoint for every sandbox container. The entire OverlayFS phase is **gated by `OVERLAY_PERSIST=1`** — when unset, the script skips straight to execd startup and the pod behaves like a standard container.

```
bootstrap.sh (PID 1, runs as root)
│
├── Phase 1: OverlayFS Setup (if OVERLAY_PERSIST=1)
│   ├── 1a. Mount overlayfs (lower=/, upper=PVC, merged=/mnt/newroot)
│   ├── 1b. Bind-mount kernel filesystems (/proc, /sys, /dev)
│   ├── 1c. Bind-mount Kubernetes volumes (/mnt/sandbox-data, /opt/opensandbox/bin)
│   ├── 1d. pivot_root (atomically swap merged view into /)
│   └── 1e. Cleanup old root (lazy unmount /.pivot_old)
│
├── Phase 2: Execd Startup
│   ├── Configure EXECD_ENVS path
│   └── Start execd in background (port 44772)
│
└── Phase 3: User Entrypoint
    ├── Drop privileges to SANDBOX_USER (if set) via su
    └── exec into user command (becomes PID 1)
```

**Key details:**

- **`pivot_root` over `chroot`** — `pivot_root` changes the mount namespace root, so `/proc/mounts` and all future mount operations see OverlayFS as the real root. `chroot` only changes path resolution.
- **`--rbind` for `/dev`** — `/dev` has sub-mounts (`/dev/pts`, `/dev/shm`, `/dev/mqueue`) from the container runtime that `--bind` alone would miss. `/proc` and `/sys` are single mounts.
- **Privilege dropping** — the container runs as root for OverlayFS setup, then drops to `SANDBOX_USER` via `su` before executing the user entrypoint. `exec` replaces the bootstrap process so the user command receives signals (e.g., `SIGTERM`) directly.
- **Failure mode** — if `mount -t overlay` or `pivot_root` fails (missing `CAP_SYS_ADMIN`, no kernel support), `set -e` causes immediate exit → `CrashLoopBackOff`.

### Environment Variables

| Variable          | Default                  | Purpose                               |
| ----------------- | ------------------------ | ------------------------------------- |
| `OVERLAY_PERSIST` | (unset)                  | `"1"` enables OverlayFS persistence   |
| `SANDBOX_USER`    | (unset)                  | Drop to this user after overlay setup |
| `EXECD`           | `/opt/opensandbox/execd` | Path to execd binary                  |
| `BOOTSTRAP_CMD`   | (unset)                  | Shell command to run as entrypoint    |

### File Locations

```
After init container copies to emptyDir:
  /opt/opensandbox/bin/execd          ← compiled Go binary
  /opt/opensandbox/bin/bootstrap.sh   ← this script

On the PVC (created at runtime):
  /mnt/sandbox-data/overlay-upper/    ← all user writes
  /mnt/sandbox-data/overlay-work/     ← kernel internal
```

## Sandbox Pod Structure

Each OpenSandbox sandbox pod runs these processes:

| Process             | Port        | Role                                                                     |
| ------------------- | ----------- | ------------------------------------------------------------------------ |
| **execd**           | 44772       | HTTP daemon — code execution, file operations, shell commands, metrics   |
| **Jupyter Server**  | 54321       | Kernel management for multi-language code interpretation                 |
| **User entrypoint** | —           | `sleep infinity` by default, or custom command                           |

### execd

Go-based HTTP daemon (Beego framework) injected into every sandbox via init container. Implements the [Execution Spec](../specs/execd-api.yaml):

- **Code execution** — routes to Jupyter kernels (Python, Java, JavaScript, TypeScript, Go, Bash)
- **Command execution** — shell commands with SSE streaming
- **File operations** — upload, download, search, permissions, move, delete
- **Metrics** — CPU/memory monitoring with SSE streaming

### Code Execution Flow

```
SDK Client                    Server (FastAPI)                 Sandbox Pod
    |                              |                              |
    | POST /sandboxes/{id}/       |                              |
    |   proxy/execd/commands      |                              |
    | ──────────────────────────> |                              |
    |                              | Forward to Pod IP:44772      |
    |                              | ───────────────────────────> |
    |                              |                              |
    |                              |              execd receives request
    |                              |                     |
    |                              |          +----------+----------+
    |                              |          |                     |
    |                              |   Shell command?        Code execution?
    |                              |          |                     |
    |                              |   Fork /bin/sh          POST to Jupyter
    |                              |   stream stdout/        kernel :54321
    |                              |   stderr via SSE              |
    |                              |          |              Jupyter runs code
    |                              |          |              returns result
    |                              |          +----------+----------+
    |                              |                     |
    |                              | <─── SSE stream ─── |
    | <─── SSE stream ─────────── |                      |
    |                              |                      |
```

**Command execution** (`sandbox.commands.run`):

1. SDK sends command string to server proxy
2. Server forwards to execd inside the pod (port 44772)
3. execd forks a shell, streams stdout/stderr back via SSE
4. SDK receives streamed output

**Code interpretation** (`interpreter.codes.run`):

1. SDK sends code + language to server proxy
2. Server forwards to execd
3. execd delegates to the Jupyter server (port 54321) which manages language kernels
4. Jupyter executes code in the appropriate kernel (Python, Java, JS, etc.)
5. Results stream back through execd → server → SDK

**File operations** (`sandbox.files.*`):

1. SDK sends file read/write/upload requests to server proxy
2. Server forwards to execd
3. execd performs filesystem operations directly and returns results

## Lifecycle State Machine

```
                    POST /sandboxes
                         |
                         v
                    +---------+
                    | Pending |
                    +----+----+
                         |
              Pod scheduled & ready
              IP assigned, execd up
                         |
                         v
                    +---------+    POST /{id}/pause    +---------+
                    | Running |  ────────────────────> | Paused  |
                    +---------+  <──────────────────── +---------+
                         |       POST /{id}/resume          |
                         |                                  |
              DELETE /   |                       DELETE /    |
              timeout    |                       timeout     |
                         v                                  v
                    +------------+                  +------------+
                    | Terminated |                  | Terminated |
                    +------------+                  +------------+

   Error at any state --> Failed
```

## Storage

### Pause / Resume (Tiered Lifecycle)

OpenSandbox uses a tiered lifecycle model for paused sandboxes. Pause is always instant; storage costs reduce automatically over time.

```
Running ──pause──► Paused (warm)  ──background──► Paused (warm+snapshot)
                       │                               │
                       │                          after N days
                       │                         (archive_after_seconds)
                       │                               │
                       │                               ▼
                       │                        Archived (cold)
                       │                      (PVC deleted, snapshot only)
                       │
                       ▼
                   Resume request
                       │
              ┌────────┴────────┐
              │                 │
         PVC exists?       PVC deleted?
              │                 │
         Mount PVC         Restore from
         (instant)         snapshot (~30-60s)
```

| Tier                | Trigger                         | Storage        | Resume Time | Cost (20Gi, AWS) |
| ------------------- | ------------------------------- | -------------- | ----------- | ---------------- |
| **Warm**            | Pause (immediate)               | PVC alive      | <5s         | ~$1.60/month     |
| **Warm+Snapshot**   | Background thread completes     | PVC + snapshot | <5s         | ~$2.60/month     |
| **Archived (cold)** | `archive_after_seconds` elapsed | Snapshot only  | ~30-60s     | ~$1.00/month     |

### Pause / Resume Flow (Production, with VolumeSnapshots)

```
PAUSE (instant):
  Server                                     K8s API
    |                                           |
    | 1. Patch CR: replicas=0, paused=true      |
    |    (returns immediately — <1s)            |
    | ────────────────────────────────────────> |
    |                                           |
    | 2. Background thread:                     |
    |    a. Create VolumeSnapshot from PVC      |
    |    b. Wait snapshot readyToUse=true       |
    |    c. Enforce retention (keep latest N)   |
    |    d. Annotate CR: snapshot-ready-at=now  |
    |    (PVC kept alive for instant resume)    |
    | ────────────────────────────────────────> |


ARCHIVE (background sweep, every 5 min):
  Server                                     K8s API
    |                                           |
    | For each paused sandbox where:            |
    |   snapshot-ready-at + archive_after_seconds < now
    |                                           |
    | 1. Delete PVC (data safe in snapshot)     |
    | ────────────────────────────────────────> |


RESUME (warm — PVC exists):
  Server                                     K8s API
    |                                           |
    | 1. Check PVC exists → yes, reuse it       |
    | 2. Patch CR: replicas=1, paused=false     |
    | ────────────────────────────────────────> |
    |                                           |
    |    Pod mounts existing PVC (~2-5s)        |


RESUME (cold — PVC archived):
  Server                                     K8s API
    |                                           |
    | 1. Check PVC exists → no                  |
    | 2. Find latest VolumeSnapshot             |
    | ────────────────────────────────────────> |
    |                                           |
    | 3. Create new PVC from snapshot           |
    |    (dataSource: VolumeSnapshot)           |
    |    Works across availability zones        |
    | ────────────────────────────────────────> |
    |                                           |
    | 4. Wait PVC bound                         |
    | 5. Patch CR: replicas=1, paused=false     |
    | ────────────────────────────────────────> |
    |                                           |
    |    Controller creates pod (~30-60s total) |
```

### Storage Mode

```
+=============================================================+
|                  PRODUCTION (EKS / GKE / AKS)               |
|                                                             |
|  CREATE:  PVC (block storage: gp3/pd-ssd/etc) + Pod        |
|  RUNNING: Pod uses PVC (no periodic snapshots)              |
|  PAUSE:   Delete Pod instantly, keep PVC                    |
|           Background: snapshot PVC, annotate ready          |
|  ARCHIVE: After archive_after_seconds: delete PVC           |
|           (data safe in snapshot)                           |
|  RESUME:  If PVC exists -> remount (instant, <5s)           |
|           If PVC archived -> restore from snapshot (~30-60s)|
|  KILL:    Delete Pod + PVC + all VolumeSnapshots + CR       |
|                                                             |
|  snapshot_enabled = true                                    |
|  snapshot_class = "csi-snapshot-class"                      |
|  archive_after_seconds = 86400  (default: 1 day)            |
+=============================================================+
```

### Kubernetes Resources per Sandbox

```
+------------------+     owns      +------------------+
| BatchSandbox CR  | ────────────> |    Pod           |
|                  |               | {id}-0           |
| annotations:     |               +--------+---------+
|   paused: "true" |                        |
|   endpoints: ... |                        | mounts
|   snapshot-       |                        v
|    ready-at: ... |               +------------------+
+--------+---------+               | PVC              |
         |                         | {id}-docker-data |
         | labels match            | 20Gi RWO         |
         v                         | (deleted after   |
+------------------+               |  archive_after_  |
| VolumeSnapshot   |  ── restore ─>|  seconds)        |
| {id}-snap-{ts}   |               +------------------+
| (0..N per sandbox)|
+------------------+
```

## Controller Reconciliation

```
Reconcile(BatchSandbox)
    |
    +── Is deleted? ──> Cleanup finalizers, done
    |
    +── Is expired? ──> Delete CR, done
    |
    +── Observe delete expectations
    |   (clear expectations for pods already gone)
    |
    +── Scale expectations satisfied?
    |       |
    |       NO ──> Requeue with backoff, done
    |       |
    |       YES
    |       v
    +── Count existing pods vs spec.replicas
    |       |
    |       +── Need more pods? ──> Create pods (scale out)
    |       |                       Set Create expectations
    |       |
    |       +── Too many pods? ──> Delete pods (scale in)
    |                              Set Delete expectations
    |
    +── Collect pod IPs ──> Update endpoints annotation
    |
    +── Update status: replicas, allocated, ready
    |
    Done
```

### Scale Expectations

The controller uses an expectations system to avoid redundant operations:

```
ExpectScale(key, Create, "pod-0")  -- I expect pod-0 to appear
ObserveScale(key, Create, "pod-0") -- pod-0 appeared, clear expectation

ExpectScale(key, Delete, "pod-0")  -- I expect pod-0 to disappear
ObserveScale(key, Delete, "pod-0") -- pod-0 is gone, clear expectation

SatisfiedExpectations(key) == true  -- all expected ops are done
                                      safe to proceed with scaling
```

## Networking

### Direct Mode

```
Client ───> NodePort (30080) ───> Server Pod (:8080)
                                      |
                                      | proxy
                                      v
                                  Sandbox Pod IP:Port
```

### Gateway Mode (3 routing strategies)

```
Client ───> Ingress Gateway ───> Sandbox Pod
                  |
                  | Route by:
                  |
                  +── Wildcard: {id}-{port}.example.com
                  +── Header:   OpenSandbox-Ingress-To: {id}-{port}
                  +── URI:      /{id}/{port}/path
                  |
                  | Lookup:
                  v
            Pod IP from CR annotation:
            sandbox.opensandbox.io/endpoints: ["10.244.0.9"]
```

### Egress Filtering (FQDN-based)

```
+====================================================================+
|                        Sandbox Pod                                  |
|                  (shared network namespace)                         |
|                                                                     |
|  +---------------------------+    +------------------------------+  |
|  | sandbox container         |    | egress sidecar               |  |
|  |                           |    |                              |  |
|  |  app does DNS lookup      |    |  DNS Proxy (:15353)          |  |
|  |  e.g. "api.github.com"   |    |    |                         |  |
|  |         |                 |    |    +── Evaluate policy       |  |
|  |         | port 53         |    |    |   (FQDN allow/deny)     |  |
|  |         |                 |    |    |                         |  |
|  +---------|─────────────────+    |    +── DENIED?               |  |
|            |                      |    |     Return NXDOMAIN     |  |
|            | iptables NAT         |    |                         |  |
|            | redirect 53→15353    |    +── ALLOWED?              |  |
|            +────────────────────> |    |     Forward upstream     |  |
|                                   |    |     Extract IPs from    |  |
|                                   |    |     A/AAAA response     |  |
|                                   |    |     Add IPs to nftables |  |
|                                   |    |     dyn_allow set (TTL) |  |
|                                   |    |                         |  |
|                                   |  nftables (inet opensandbox) |  |
|                                   |    |                         |  |
|                                   |    +── established: ACCEPT   |  |
|                                   |    +── loopback: ACCEPT      |  |
|                                   |    +── IP in allow set: ACCEPT|  |
|                                   |    +── IP in deny set: DROP  |  |
|                                   |    +── default: DROP         |  |
|                                   +------------------------------+  |
|                                                                     |
|  app TCP connect (resolved IP)                                      |
|         |                                                           |
|         +── nftables checks IP ── ALLOW ──> internet                |
|                                ── DROP  ──> blocked                 |
+====================================================================+
```

Two enforcement layers at different network levels:
- **DNS proxy (L7 — Application)** — filters domain names, returns NXDOMAIN for denied FQDNs
- **nftables (L3/L4 — Network/Transport)** — filters IPs learned from DNS responses, blocks direct-IP bypass

Both layers are needed: DNS alone can be bypassed by hardcoding IPs; nftables alone can't match domain names since IPs change. The DNS proxy resolves FQDNs to IPs dynamically and feeds them into nftables `dyn_allow` sets with TTL.

Policy is injected via `OPENSANDBOX_EGRESS_RULES` env var and can be hot-reloaded at runtime via the sidecar's `/policy` HTTP endpoint.

## Resource Profile

Per-sandbox resource consumption:

| Component         | CPU (idle) | CPU (active) | Memory     |
| ----------------- | ---------- | ------------ | ---------- |
| Sandbox container | 50-100m    | 0.5-4 vCPU   | 512Mi-4Gi  |
| execd daemon      | ~10m       | ~50m         | 5-20 MB    |
| Jupyter server    | ~50m       | 200m-1 vCPU  | 100-300 MB |
| Egress sidecar    | ~10m       | ~50m         | 20-50 MB   |

**Recommended sizing for AI coding agents:** 1-2 vCPU, 2-4 Gi memory, 15-20 GB ephemeral storage.

## Configuration Reference

```toml
[server]
host = "0.0.0.0"
port = 8080
log_level = "INFO"
api_key = ""                    # optional

[runtime]
type = "kubernetes"
execd_image = "opensandbox/execd:dev"

[kubernetes]
namespace = "opensandbox"
workload_provider = "batchsandbox"
batchsandbox_template_file = "/etc/opensandbox/batchsandbox-template.yaml"
informer_enabled = true
informer_resync_seconds = 300
informer_watch_timeout_seconds = 60

# Storage
storage_class = ""              # empty = cluster default
storage_size = "20Gi"

# Snapshots (production only)
snapshot_enabled = false
snapshot_class = ""             # e.g. "csi-snapshot-class"
snapshot_max_retention = 1      # keep only the latest pause snapshot
archive_after_seconds = 86400   # seconds before PVC is deleted (0 = immediate)

[ingress]
mode = "direct"                 # or "gateway"

[storage]
allowed_host_paths = []
```

## RBAC Requirements

```yaml
rules:
  - apiGroups: [""]
    resources: [pods, pods/log, pods/status, services, events]
    verbs: [get, list, watch, create, update, patch, delete]
  - apiGroups: [""]
    resources: [namespaces]
    verbs: [get, list]
  - apiGroups: [""]
    resources: [persistentvolumeclaims]
    verbs: [get, list, create, delete]
  - apiGroups: [snapshot.storage.k8s.io]
    resources: [volumesnapshots]
    verbs: [get, list, create, delete]
  - apiGroups: [sandbox.opensandbox.io]
    resources: [batchsandboxes, batchsandboxes/status]
    verbs: [get, list, watch, create, update, patch, delete]
```

## Cloud Prerequisites for VolumeSnapshot Support

| Cloud            | StorageClass             | Snapshot Driver         | VolumeSnapshotClass       |
| ---------------- | ------------------------ | ----------------------- | ------------------------- |
| **EKS**          | `gp3` (EBS CSI)          | `ebs.csi.aws.com`       | EBS snapshot class        |
| **GKE**          | `pd-ssd` / `pd-balanced` | `pd.csi.storage.gke.io` | GCE PD snapshot class     |
| **AKS**          | `managed-premium`        | `disk.csi.azure.com`    | Azure Disk snapshot class |
| **Self-managed** | Any CSI driver           | Provider-specific       | Provider-specific         |

All use the standard `snapshot.storage.k8s.io/v1` API. No OpenSandbox code changes needed per cloud.
