#!/bin/bash

# Copyright 2025 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

set -e

# --- OverlayFS full-filesystem persistence ---
# Skip overlay setup on capsh re-exec (overlay is already mounted).
if [ "${OVERLAY_PERSIST:-}" = "1" ] && [ "${_CAPS_DROPPED:-}" != "1" ]; then
    SANDBOX_DATA="/mnt/sandbox-data"
    UPPER="$SANDBOX_DATA/overlay-upper"
    WORK="$SANDBOX_DATA/overlay-work"
    MERGED="/mnt/newroot"

    mkdir -p "$UPPER" "$WORK" "$MERGED"
    # Upper dir needs 755 so non-root users can access files through overlay.
    # Work dir is kernel-internal and can be restricted.
    chmod 755 "$UPPER"
    chmod 700 "$WORK"
    chown root:root "$UPPER" "$WORK"

    # Clean stale overlay-work from previous unclean shutdown.
    # Kernel requires work dir to be empty for overlay mount.
    rm -rf "${WORK:?}"/* 2>/dev/null || true

    # Mount overlayfs: lower=current rootfs, upper=PVC, merged=new root
    mount -t overlay overlay \
        -o "lowerdir=/,upperdir=$UPPER,workdir=$WORK" \
        "$MERGED"

    # Bind-mount critical kernel filesystems into merged root
    mount --bind /proc "$MERGED/proc"
    mount --bind /sys "$MERGED/sys"
    # Recursive bind for /dev to capture submounts (/dev/pts, /dev/shm, etc.)
    mount --rbind /dev "$MERGED/dev"

    # Bind-mount Kubernetes volumes into the merged root.
    # These are not part of the container image so overlayfs lower layer
    # does not include them — they must be propagated explicitly.
    for mp in /mnt/sandbox-data /opt/opensandbox/bin; do
        if mountpoint -q "$mp" 2>/dev/null || [ -d "$mp" ]; then
            mkdir -p "$MERGED$mp"
            mount --bind "$mp" "$MERGED$mp"
        fi
    done

    # Bind-mount DNS config so the sandbox can resolve external hostnames.
    # Kubernetes injects /etc/resolv.conf with cluster DNS (e.g. 10.96.0.10)
    # but overlayfs lower layer may contain a stale image copy.
    if [ -f /etc/resolv.conf ]; then
        cp /etc/resolv.conf "$MERGED/etc/resolv.conf"
    fi

    # pivot_root: swap merged root into place
    cd "$MERGED"
    mkdir -p .pivot_old
    pivot_root . .pivot_old

    # Unmount old root (lazy to handle busy mounts) and clean up mountpoint
    umount -l /.pivot_old 2>/dev/null || true
    rmdir /.pivot_old 2>/dev/null || true

    # Harden sensitive /etc file permissions in the overlay root
    chmod 644 /etc/passwd /etc/group /etc/hosts /etc/resolv.conf 2>/dev/null || true
    chmod 640 /etc/shadow 2>/dev/null || true
    chown root:root /etc/passwd /etc/group /etc/hosts /etc/resolv.conf 2>/dev/null || true
    chown root:shadow /etc/shadow 2>/dev/null || true

    # Remove dangerous sudoers entries so even root cannot escalate via sudo
    if [ -f /etc/sudoers ]; then
        sed -i '/^root\s/d; /^%admin\s/d; /^%sudo\s/d' /etc/sudoers 2>/dev/null || true
    fi

    # Harden sensitive directories
    chmod 755 /opt/opensandbox/bin 2>/dev/null || true
    chown root:root /opt/opensandbox/bin 2>/dev/null || true
    chmod 750 /mnt/sandbox-data 2>/dev/null || true
    chown root:root /mnt/sandbox-data 2>/dev/null || true

    # SUID/SGID stripping and world-writable fixes are applied at image
    # build time (Dockerfile steps 21/23).  The overlay lower layer inherits
    # those permissions so repeating the scan here is unnecessary.

    # Harden kernel tunables where possible (best-effort, may be read-only in container)
    echo 1 > /proc/sys/kernel/unprivileged_bpf_disabled 2>/dev/null || true
    echo 1 > /proc/sys/kernel/kptr_restrict 2>/dev/null || true
    echo 0 > /proc/sys/net/ipv4/conf/all/accept_redirects 2>/dev/null || true
    echo 0 > /proc/sys/net/ipv4/conf/default/accept_redirects 2>/dev/null || true

    # --- Setup files that need CHOWN before capsh drops it ---
    _setup_execd_files() {
        EXECD="${EXECD:=/opt/opensandbox/execd}"
        if [ -z "${EXECD_ENVS:-}" ]; then
            EXECD_ENVS="/opt/opensandbox/.env"
        fi
        mkdir -p "$(dirname "$EXECD_ENVS")" 2>/dev/null || true
        touch "$EXECD_ENVS" 2>/dev/null || true
        if [ -n "${SANDBOX_USER:-}" ] && [ "$(id -u)" = "0" ] && [ "$SANDBOX_USER" != "root" ]; then
            chown "$SANDBOX_USER" "$EXECD_ENVS" 2>/dev/null || true
        fi
        export EXECD_ENVS

        if [ -n "${EXECD_ACCESS_TOKEN:-}" ]; then
            _token_file="/run/opensandbox-execd-token"
            printf '%s' "$EXECD_ACCESS_TOKEN" > "$_token_file"
            chmod 600 "$_token_file"
            if [ -n "${SANDBOX_USER:-}" ] && [ "$SANDBOX_USER" != "root" ]; then
                chown "$SANDBOX_USER" "$_token_file" 2>/dev/null || chmod 640 "$_token_file"
            fi
            export EXECD_ACCESS_TOKEN_FILE="$_token_file"
            unset EXECD_ACCESS_TOKEN
        fi
    }
    _setup_execd_files

    # Drop setup-only capabilities from bounding set now that overlay is complete.
    # SYS_ADMIN, CHOWN, DAC_OVERRIDE, FOWNER, SETPCAP are only needed during setup.
    # This leaves SETUID, SETGID, KILL in the bounding set for normal operation.
    if command -v capsh >/dev/null 2>&1 && [ "${_CAPS_DROPPED:-}" != "1" ]; then
        export _CAPS_DROPPED=1
        exec capsh \
            --drop=cap_sys_admin,cap_chown,cap_dac_override,cap_fowner,cap_setpcap \
            -- -c 'exec "$@"' -- "$0" "$@"
    fi

    echo "OverlayFS filesystem persistence enabled."
fi
# --- End OverlayFS ---

EXECD="${EXECD:=/opt/opensandbox/execd}"

# Non-overlay path: set up execd files (overlay path handled above before capsh).
if [ "${_CAPS_DROPPED:-}" != "1" ] && [ -z "${EXECD_ACCESS_TOKEN_FILE:-}" ]; then
    if [ -z "${EXECD_ENVS:-}" ]; then
        EXECD_ENVS="/opt/opensandbox/.env"
    fi
    mkdir -p "$(dirname "$EXECD_ENVS")" 2>/dev/null || true
    touch "$EXECD_ENVS" 2>/dev/null || true
    if [ -n "${SANDBOX_USER:-}" ] && [ "$(id -u)" = "0" ] && [ "$SANDBOX_USER" != "root" ]; then
        chown "$SANDBOX_USER" "$EXECD_ENVS" 2>/dev/null || true
    fi
    export EXECD_ENVS

    if [ -n "${EXECD_ACCESS_TOKEN:-}" ]; then
        _token_file="/run/opensandbox-execd-token"
        printf '%s' "$EXECD_ACCESS_TOKEN" > "$_token_file"
        chmod 600 "$_token_file"
        if [ -n "${SANDBOX_USER:-}" ] && [ "$SANDBOX_USER" != "root" ]; then
            chown "$SANDBOX_USER" "$_token_file" 2>/dev/null || chmod 640 "$_token_file"
        fi
        export EXECD_ACCESS_TOKEN_FILE="$_token_file"
        unset EXECD_ACCESS_TOKEN
    fi
fi

echo "starting OpenSandbox Execd daemon at $EXECD."
# Launch execd in a supervised restart loop. The loop subshell runs as
# root so the sandbox user cannot kill it; only the execd child inside
# runs as SANDBOX_USER.
_launch_execd() {
	# Set HOME to the sandbox user's home directory so child processes
	# (e.g. uv, pip) use writable cache paths instead of /root.
	if [ -n "${SANDBOX_USER:-}" ] && [ "$SANDBOX_USER" != "root" ]; then
		HOME="$(getent passwd "$SANDBOX_USER" | cut -d: -f6)"
		export HOME
	fi
	local _max=100 _n=0
	while [ "$_n" -lt "$_max" ]; do
		if [ -n "${SANDBOX_USER:-}" ] && [ "$(id -u)" = "0" ] && [ "$SANDBOX_USER" != "root" ]; then
			setpriv --reuid="$(id -u "$SANDBOX_USER")" \
				--regid="$(id -g "$SANDBOX_USER")" \
				--init-groups $EXECD || true
		else
			$EXECD || true
		fi
		_n=$((_n + 1))
		echo "execd exited, restart $_n/$_max in 1s..." >&2
		sleep 1
	done
	echo "execd restart limit reached." >&2
}
_launch_execd &

# Allow chained shell commands (e.g., /test1.sh && /test2.sh)
# Usage:
#   bootstrap.sh -c "/test1.sh && /test2.sh"
# Or set BOOTSTRAP_CMD="/test1.sh && /test2.sh"
CMD=""
if [ "${BOOTSTRAP_CMD:-}" != "" ]; then
	CMD="$BOOTSTRAP_CMD"
elif [ $# -ge 1 ] && [ "$1" = "-c" ]; then
	shift
	CMD="$*"
fi

# If SANDBOX_USER is set and we are root, drop privileges for the user process.
# This handles the case where runAsUser=0 was forced for OverlayFS setup.
# Uses chroot-style su which preserves the environment and working directory.
_exec_as_user() {
	if [ -n "${SANDBOX_USER:-}" ] && [ "$(id -u)" = "0" ] && [ "$SANDBOX_USER" != "root" ]; then
		_uid=$(id -u "$SANDBOX_USER")
		_gid=$(id -g "$SANDBOX_USER")
		exec setpriv --reuid="$_uid" --regid="$_gid" --init-groups "$@"
	else
		exec "$@"
	fi
}

# Clear token env vars before dropping to user process
unset EXECD_ACCESS_TOKEN EXECD_ACCESS_TOKEN_FILE

if [ "$CMD" != "" ]; then
	_exec_as_user bash -c "$CMD"
fi

if [ $# -eq 0 ]; then
	_exec_as_user bash
fi

_exec_as_user "$@"
