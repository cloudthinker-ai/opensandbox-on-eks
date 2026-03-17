"""Shared helper to detect pod-level failure signals from Kubernetes pod objects."""

from typing import Optional

# Container waiting reasons that indicate unrecoverable failure.
# Grouped by category for clarity.
_FATAL_WAITING_REASONS: dict[str, tuple[str, str]] = {
    # Image issues — image cannot be pulled or doesn't exist
    "ImagePullBackOff":           ("IMAGE_PULL_FAILED", "Container image pull failed"),
    "ErrImagePull":               ("IMAGE_PULL_FAILED", "Container image pull failed"),
    "InvalidImageName":           ("IMAGE_PULL_FAILED", "Invalid container image name"),
    # Crash loop — container starts but keeps crashing
    "CrashLoopBackOff":           ("CRASH_LOOP", "Container is in CrashLoopBackOff"),
    # Config errors — missing secrets, configmaps, or bad spec
    "CreateContainerConfigError": ("CONTAINER_CONFIG_ERROR", "Container configuration error"),
    "CreateContainerError":       ("CONTAINER_CREATE_ERROR", "Container creation error"),
    # Runtime errors
    "RunContainerError":          ("CONTAINER_RUNTIME_ERROR", "Container runtime error"),
}


def detect_pod_failure(pod) -> Optional[tuple[str, str, str]]:
    """Inspect a single V1Pod for terminal failure signals.

    Returns:
        A ``(state, reason, message)`` tuple if a failure is detected,
        or ``None`` otherwise.
    """
    if not pod.status:
        return None

    if pod.status.phase == "Failed":
        message = pod.status.reason or "Pod entered Failed phase"
        return ("Failed", "POD_FAILED", message)

    all_statuses = list(pod.status.container_statuses or []) + list(
        pod.status.init_container_statuses or []
    )
    for cs in all_statuses:
        if cs.state and cs.state.waiting:
            waiting_reason = cs.state.waiting.reason or ""
            fatal = _FATAL_WAITING_REASONS.get(waiting_reason)
            if fatal:
                reason_code, default_msg = fatal
                msg = cs.state.waiting.message or default_msg
                return ("Failed", reason_code, f"{cs.name}: {msg}")
        if cs.state and cs.state.terminated:
            if cs.state.terminated.reason == "OOMKilled":
                return (
                    "Failed",
                    "OOM_KILLED",
                    f"{cs.name}: OOMKilled (exit code {cs.state.terminated.exit_code})",
                )

    return None
