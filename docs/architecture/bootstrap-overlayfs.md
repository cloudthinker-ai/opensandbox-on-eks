# bootstrap.sh — OverlayFS Filesystem Persistence

`bootstrap.sh` is the PID 1 entrypoint for every sandbox container. It sets up filesystem persistence via OverlayFS (when enabled), starts the execd daemon, and then hands off to the user's entrypoint.

## Execution Flow

```
bootstrap.sh (PID 1, runs as root)
│
├── Phase 1: OverlayFS Setup (if OVERLAY_PERSIST=1)
│   ├── 1a. Mount overlayfs
│   ├── 1b. Bind-mount kernel filesystems
│   ├── 1c. Bind-mount Kubernetes volumes
│   ├── 1d. pivot_root (swap root)
│   └── 1e. Cleanup old root
│
├── Phase 2: Execd Startup
│   ├── Configure EXECD_ENVS path
│   └── Start execd in background
│
└── Phase 3: User Entrypoint
    ├── Drop privileges (if SANDBOX_USER set)
    └── exec into user command
```

## Phase 1: OverlayFS Setup

This entire phase is **gated by `OVERLAY_PERSIST=1`**. When the env var is not set, bootstrap.sh skips straight to Phase 2 — behavior is identical to the original.

### 1a. Mount OverlayFS

```bash
mount -t overlay overlay \
    -o "lowerdir=/,upperdir=$UPPER,workdir=$WORK" \
    "$MERGED"
```

This is the core operation. It creates a union filesystem:

```
MERGED (/mnt/newroot) = LOWER (/) + UPPER (PVC:/overlay-upper)

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

**Why this works for persistence:** The UPPER directory lives on the PVC. When the pod is killed (pause), the PVC survives. When the pod is recreated (resume), the same PVC is remounted, and overlayfs sees all previous writes in UPPER.

**`set -e` behavior:** If `mount -t overlay` fails (kernel doesn't support it, or `CAP_SYS_ADMIN` is missing), the script exits immediately. The container enters `CrashLoopBackOff` — a clear error signal rather than running silently without persistence.

### 1b. Bind-mount Kernel Filesystems

```bash
mount --bind /proc "$MERGED/proc"
mount --bind /sys "$MERGED/sys"
mount --rbind /dev "$MERGED/dev"       # recursive!
```

After mounting overlayfs, `/mnt/newroot` has a copy of the original rootfs file tree, but **virtual kernel filesystems are not part of the image**. They must be explicitly propagated into the merged root:

| Filesystem | Mount type | Why |
|------------|------------|-----|
| `/proc` | `--bind` | Process info, `/proc/self`, required by nearly everything |
| `/sys` | `--bind` | Kernel parameters, device info |
| `/dev` | `--rbind` | Device nodes. **Recursive** because `/dev/pts` (pseudoterminals), `/dev/shm` (shared memory), `/dev/mqueue` are separate sub-mounts that `--bind` alone would miss |

**Why `--rbind` for `/dev` only:** `/proc` and `/sys` are single mounts with no sub-mounts in a container context. `/dev` has multiple sub-mounts from the container runtime (containerd/Docker) that must all be visible.

### 1c. Bind-mount Kubernetes Volumes

```bash
for mp in /mnt/sandbox-data /opt/opensandbox/bin; do
    if mountpoint -q "$mp" 2>/dev/null || [ -d "$mp" ]; then
        mkdir -p "$MERGED$mp"
        mount --bind "$mp" "$MERGED$mp"
    fi
done
```

Kubernetes volume mounts exist **outside the container image**. The overlayfs lower layer is a snapshot of the image rootfs — it does not include runtime volume mounts. So these must be explicitly bind-mounted into the merged root:

| Volume | Source | Why it's needed |
|--------|--------|-----------------|
| `/mnt/sandbox-data` | PVC (full root) | Bootstrap.sh needs access to `overlay-upper/` after pivot_root |
| `/opt/opensandbox/bin` | emptyDir (from init container) | Contains `execd` binary and `bootstrap.sh` — init container copies these here at pod startup |

### 1d. pivot_root — Swap the Root Filesystem

```bash
cd "$MERGED"
mkdir -p .pivot_old
pivot_root . .pivot_old
```

`pivot_root` is a Linux syscall that atomically swaps the root filesystem:

```
BEFORE pivot_root:
  /          = original container rootfs (ephemeral)
  /mnt/newroot = overlayfs merged view

AFTER pivot_root:
  /          = overlayfs merged view (what we want!)
  /.pivot_old = original container rootfs (to be unmounted)
```

**Why `pivot_root` instead of `chroot`:** `chroot` only changes the apparent root for path resolution. `pivot_root` actually changes the mount namespace root. This means `/proc/mounts` and all future mount operations see the overlayfs as the real root. Tools like `mount`, `df`, and container runtimes inside the sandbox work correctly.

**Why `cd "$MERGED"` first:** `pivot_root` requires the calling process to be inside the new root directory. If the process's cwd is outside, the syscall fails.

### 1e. Cleanup Old Root

```bash
umount -l /.pivot_old 2>/dev/null || true
rmdir /.pivot_old 2>/dev/null || true
```

After pivot_root, the old rootfs is accessible at `/.pivot_old`. We unmount it to free resources:

- `umount -l` (lazy unmount): Detaches the mount immediately but defers cleanup until no process has open file handles on it. This is necessary because the kernel may still have references (e.g., from `/proc` or pending I/O).
- `rmdir`: Removes the empty mountpoint directory. Prevents users from seeing a mysterious `/.pivot_old` directory.
- `|| true`: Both commands are best-effort. If they fail (e.g., busy mount), the system still works — the old root just consumes some memory until the container exits.

## Phase 2: Execd Startup

```bash
EXECD="${EXECD:=/opt/opensandbox/execd}"
# ... setup EXECD_ENVS ...
$EXECD &
```

Execd is the OpenSandbox daemon that accepts remote command execution requests. It runs in the background (`&`) so bootstrap.sh can continue to `exec` the user entrypoint.

Key points:
- `EXECD` defaults to `/opt/opensandbox/execd` but can be overridden
- `EXECD_ENVS` points to an env file that execd reads for configuration
- The `mkdir -p` and `touch` around `EXECD_ENVS` are best-effort — if they fail, execd still starts but without the env file
- Execd runs **after** overlay setup, so it sees the persistent filesystem

## Phase 3: User Entrypoint

```bash
_exec_as_user() {
    if [ -n "${SANDBOX_USER:-}" ] && [ "$(id -u)" = "0" ] && [ "$SANDBOX_USER" != "root" ]; then
        # Build quoted command, switch to SANDBOX_USER via su
        exec su -s /bin/sh "$SANDBOX_USER" -c "$_cmd"
    else
        exec "$@"
    fi
}
```

The entrypoint supports three invocation modes:

| Mode | How | Example |
|------|-----|---------|
| `BOOTSTRAP_CMD` env | Set env var | `BOOTSTRAP_CMD="/test1.sh && /test2.sh"` |
| `-c` flag | Pass as argument | `bootstrap.sh -c "/test1.sh && /test2.sh"` |
| Direct args | Pass command directly | `bootstrap.sh sleep infinity` |

**Privilege dropping via `SANDBOX_USER`:**

When `filesystem_persistence` is enabled, the container is forced to `runAsUser: 0` (root) because OverlayFS mount and pivot_root require root. But the user's image may expect to run as a non-root user (e.g., `USER user` in Dockerfile).

If `SANDBOX_USER` is set:
1. OverlayFS setup runs as root (Phase 1)
2. Execd starts as root (Phase 2)
3. User entrypoint runs as `$SANDBOX_USER` via `su` (Phase 3)

The `exec` in `_exec_as_user` replaces the bootstrap.sh process — the user command becomes PID 1 (or inherits it via su). This is important for signal handling: `SIGTERM` from Kubernetes goes directly to the user process for graceful shutdown.

## Environment Variables Reference

| Variable | Default | Set by | Purpose |
|----------|---------|--------|---------|
| `OVERLAY_PERSIST` | (unset) | Server (provider) | `"1"` enables OverlayFS persistence |
| `SANDBOX_USER` | (unset) | User / provider | Drop to this user after overlay setup |
| `EXECD` | `/opt/opensandbox/execd` | Image | Path to execd binary |
| `EXECD_ENVS` | `/opt/opensandbox/.env` | Image | Path to execd env file |
| `BOOTSTRAP_CMD` | (unset) | User | Shell command to run as entrypoint |

## Failure Modes

| Failure | Cause | Behavior |
|---------|-------|----------|
| `mount -t overlay` fails | Missing `CAP_SYS_ADMIN` or kernel support | `set -e` → script exits → `CrashLoopBackOff` |
| `pivot_root` fails | Not inside merged dir, or permission denied | `set -e` → script exits → `CrashLoopBackOff` |
| `execd` not found | Init container didn't copy it, or bind-mount failed | `set -e` → script exits → `CrashLoopBackOff` |
| `SANDBOX_USER` invalid | User doesn't exist in image | `su` fails → `set -e` may not catch (after `set -x`) |
| `OVERLAY_PERSIST` unset | `filesystem_persistence=false` | No overlay — entire Phase 1 skipped, behaves like original |

## File Locations

```
In the execd image (before init container copies):
  /execd              ← compiled Go binary
  /bootstrap.sh       ← this script

After init container copies to emptyDir:
  /opt/opensandbox/bin/execd
  /opt/opensandbox/bin/bootstrap.sh

On the PVC (created at runtime):
  /mnt/sandbox-data/overlay-upper/    ← all user writes
  /mnt/sandbox-data/overlay-work/     ← kernel internal
```
