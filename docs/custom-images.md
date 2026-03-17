# Custom Sandbox Images

How to build your own sandbox images for OpenSandbox.

## Overview

Any Docker image can be a sandbox. When you create a sandbox, OpenSandbox:

1. Starts an **init container** that copies `execd` (the execution daemon) and `bootstrap.sh` into a shared volume at `/opt/opensandbox/bin/`
2. Starts your image as the **main container** with `bootstrap.sh` as the entrypoint
3. `bootstrap.sh` sets up OverlayFS (if enabled), starts `execd` in the background, drops privileges, and execs your entrypoint

Your image just needs standard Linux tools. You don't need to install execd or bootstrap — they're injected automatically.

## How the boot sequence works

```
Pod starts
│
├─ Init container (execd image)
│   └─ Copies /execd and /bootstrap.sh → /opt/opensandbox/bin/
│
└─ Main container (YOUR image)
    └─ Entrypoint: /opt/opensandbox/bin/bootstrap.sh <your-entrypoint>
        │
        ├─ Phase 1: OverlayFS setup (if filesystem_persistence=true)
        │   ├─ Mount overlay: rootfs + PVC upper → merged root
        │   ├─ Bind-mount /proc, /sys, /dev
        │   ├─ pivot_root to swap merged root into place
        │   └─ Drop CAP_SYS_ADMIN via capsh
        │
        ├─ Phase 2: Start execd daemon (background, port 44772)
        │
        ├─ Phase 3: Security hardening
        │   ├─ chmod /etc/passwd, /etc/shadow
        │   ├─ Remove dangerous sudoers entries
        │   └─ Harden kernel tunables
        │
        └─ Phase 4: Drop privileges and exec user entrypoint
            └─ setpriv --reuid=SANDBOX_USER (if configured)
```

## Image requirements

### Required

| Requirement | Details |
|-------------|---------|
| Base OS | Any Linux distribution (Ubuntu, Debian, Alpine, etc.) |
| `/bin/bash` | bootstrap.sh is a bash script |
| `/bin/sh` | Used for shell-mode command wrapping |
| `id`, `getent` | User lookup (`id -u`, `getent passwd`) |
| Standard filesystem | `/proc`, `/sys`, `/dev`, `/etc/passwd`, `/etc/group` |
| Architecture | `linux/amd64` or `linux/arm64` |

### Required for OverlayFS persistence

These are only needed when `filesystem_persistence: true` in the server config (the default for EKS deployments):

| Requirement | Package | Details |
|-------------|---------|---------|
| `mount`, `pivot_root`, `umount` | `util-linux` | Filesystem setup |
| `setpriv` | `util-linux` | Privilege drop to non-root user |
| `capsh` | `libcap2-bin` (Debian/Ubuntu) or `libcap` (Alpine) | Capability drop after overlay setup |
| `chown`, `chmod` | `coreutils` | File permission hardening |

### Not required

- **execd** — injected automatically via init container
- **bootstrap.sh** — injected automatically via init container
- **ENTRYPOINT / CMD in Dockerfile** — OpenSandbox overrides these with bootstrap.sh. Don't set them (they'll be ignored).

## Minimal example

### Ubuntu-based

```dockerfile
FROM ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    curl \
    wget \
    git \
    ca-certificates \
    util-linux \
    libcap2-bin \
    && rm -rf /var/lib/apt/lists/*

# Add your tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Optional: create a non-root user for SANDBOX_USER
RUN useradd -m -s /bin/bash sandboxuser
```

### Alpine-based

```dockerfile
FROM alpine:3.20

RUN apk add --no-cache \
    bash \
    curl \
    wget \
    git \
    ca-certificates \
    util-linux \
    libcap

# Add your tools
RUN apk add --no-cache python3 py3-pip

# Optional: create a non-root user for SANDBOX_USER
RUN adduser -D -s /bin/bash sandboxuser
```

### Node.js development

```dockerfile
FROM node:20

# Node.js images already include bash, util-linux
# Just add capsh for OverlayFS mode
RUN apt-get update && apt-get install -y --no-install-recommends \
    libcap2-bin \
    && rm -rf /var/lib/apt/lists/*

# Pre-install your project dependencies
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
```

## Using a non-root user (SANDBOX_USER)

When OverlayFS persistence is enabled, the sandbox pod starts as **root** (uid 0) because `mount` and `pivot_root` require `CAP_SYS_ADMIN`. After the OverlayFS setup completes, `bootstrap.sh` drops privileges to the user specified by the `SANDBOX_USER` environment variable.

The flow:

```
root (PID 1, bootstrap.sh)
  → OverlayFS mount + pivot_root
  → capsh drops CAP_SYS_ADMIN
  → setpriv --reuid=sandboxuser --regid=sandboxuser
  → exec <user's entrypoint>  (now running as sandboxuser)
```

**Requirements:**

- The user must exist in the image's `/etc/passwd` (create with `useradd` or `adduser` in your Dockerfile)
- The user's home directory must exist (created automatically by `useradd -m`)
- `bootstrap.sh` uses `getent passwd $SANDBOX_USER` to find the home directory and sets `HOME` accordingly

If `SANDBOX_USER` is not set, the user's code runs as root inside the sandbox.

## Build, push, and use

### Build your image

```bash
docker build -t my-sandbox:latest .
```

### Push to ECR

```bash
# Authenticate
aws ecr get-login-password --region <REGION> | \
  docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com

# Create repository
aws ecr create-repository --repository-name my-sandbox --region <REGION> || true

# Tag and push
docker tag my-sandbox:latest <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/my-sandbox:latest
docker push <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/my-sandbox:latest
```

### Use in API call

```bash
curl -X POST "http://localhost:8080/v1/sandboxes" \
  -H "OPEN-SANDBOX-API-KEY: <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "image": {
      "uri": "<ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/my-sandbox:latest",
      "auth": { "username": "AWS", "password": "<ecr-token>" }
    },
    "entrypoint": ["python3", "-c", "print(\"hello from custom image\")"],
    "timeout": 3600,
    "resourceLimits": { "cpu": "500m", "memory": "512Mi" }
  }'
```

For public images (e.g., `python:3.11-slim`), omit the `auth` field.

## Using public images directly

Many public images work out of the box. Here are tested examples:

| Image | Use case | Notes |
|-------|----------|-------|
| `python:3.11-slim` | Python development | Includes bash, util-linux |
| `python:3.11` | Python with build tools | Includes gcc, make, etc. |
| `node:20` | Node.js development | Includes bash, util-linux |
| `ubuntu:24.04` | General purpose | Install `util-linux` and `libcap2-bin` for OverlayFS |
| `debian:bookworm-slim` | Lightweight general purpose | Install `util-linux` and `libcap2-bin` for OverlayFS |

**Alpine images** need extra packages for OverlayFS mode:

```bash
apk add --no-cache bash util-linux libcap
```

## Common pitfalls

| Problem | Cause | Fix |
|---------|-------|-----|
| `bootstrap.sh: not found` | Image doesn't have `/bin/bash` | Install `bash` in your Dockerfile |
| `mount: permission denied` | Missing `CAP_SYS_ADMIN` | Ensure namespace allows privileged pods (pod security labels) |
| `setpriv: unknown user` | `SANDBOX_USER` doesn't exist in image | Add `RUN useradd -m -s /bin/bash <user>` to Dockerfile |
| `capsh: command not found` | Missing capability tool | Install `libcap2-bin` (Debian/Ubuntu) or `libcap` (Alpine) |
| `pivot_root: No such file or directory` | Missing mount utilities | Install `util-linux` |
| ENTRYPOINT ignored | OpenSandbox overrides it | Don't rely on Dockerfile ENTRYPOINT; pass entrypoint in API call |
| `sudo: command not found` | sudoers removed at runtime | Don't rely on sudo in sandbox code |

## Environment variables set by the system

These are automatically set by the server when creating the sandbox pod:

| Variable | Description |
|----------|-------------|
| `OVERLAY_PERSIST` | `1` if filesystem persistence is enabled |
| `SANDBOX_USER` | Non-root user to drop privileges to (if configured) |
| `EXECD` | Path to execd binary (`/opt/opensandbox/execd`) |
| `EXECD_ENVS` | Path to execd config file |
| `EXECD_ACCESS_TOKEN` | Auth token for execd API (cleared before user process starts) |
| `SANDBOX_DATA` | PVC mount point (`/mnt/sandbox-data`, OverlayFS mode only) |

## Reference: code-interpreter image

The [`sandboxes/code-interpreter/`](../sandboxes/code-interpreter/) directory contains a full-featured reference sandbox image with:

- Python 3.10–3.14 (switchable via `PYTHON_VERSION` env var)
- Node.js 18, 20, 22
- Go 1.23–1.25
- Java 8, 11, 17, 21 with Maven
- Jupyter with kernels for all languages
- Cloud CLIs, build tools, and development utilities

Use it as inspiration for your own images, but most use cases need far less — a basic Ubuntu or Python image is usually enough.
