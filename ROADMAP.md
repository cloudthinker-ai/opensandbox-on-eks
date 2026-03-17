# OpenSandbox Roadmap

This roadmap tracks what's been built, what's actively being developed, and what's coming next. For details on any item, follow its linked OSEP or design doc.

> Priorities shift as the project evolves. This is a living document — not a commitment schedule.

---

## Done


| Feature                    | Description                                                                                                 |
| -------------------------- | ----------------------------------------------------------------------------------------------------------- |
| Sandbox Lifecycle API      | Create, pause, resume, terminate sandboxes                                                                  |
| Code Execution             | Multi-language execution via Jupyter kernels (Python, Java, JS, TS, Go, Bash)                               |
| Command Execution          | Shell command execution with SSE streaming                                                                  |
| File Operations            | Upload, download, search, permissions, move, delete                                                         |
| Kubernetes Runtime         | BatchSandbox CRD, controller reconciliation, scale expectations                                             |
| Docker Runtime             | Local Docker-based runtime for development                                                                  |
| OverlayFS Persistence      | Filesystem persistence via PVC-backed upper layer                                                           |
| Pause/Resume Storage Tiers | Warm, snapshot, and archived tiers with VolumeSnapshots                                                     |
| Egress Control             | Per-sandbox egress sidecar with FQDN-based filtering |
| Ingress Routing            | Direct + gateway mode (wildcard, header, URI strategies)                                                    |
| Pool System                | Warm pod pool for instant sandbox creation                                                                  |
| Multi-Language SDKs        | Python, JavaScript/TypeScript, Java/Kotlin, C#/.NET                                                         |
| Helm Chart                 | Production Helm chart with configurable values                                                              |
| Metrics                    | CPU/memory metrics with SSE watch streams                                                                   |
| Image Update Strategy      | Sidecar image updates and egress injection across redeployments                                             |
| Volume Support             | First-class volume resources with mount semantics                                                           |


## Up Next


| Feature                  | Description                                                                                                     |
| ------------------------ | --------------------------------------------------------------------------------------------------------------- |
| Pool CR                  | Warm pod pool with persistent volume support for instant sandbox creation |
| Secure Container Runtime | gVisor, Kata Containers, and Firecracker runtime support |
| Tiered Cold Storage      | Disk metrics + optional EFS-backed `/cold` mount for client-managed archival |


## Future

Ideas under consideration — not yet committed.


| Feature                   | Description                                                             |
| ------------------------- | ----------------------------------------------------------------------- |
| Local Lightweight Sandbox | Lightweight local sandbox for development and testing (no K8s required) |


---

## How We Track Work

- **GitHub Issues** — Bug reports, features, and tasks are tracked as issues.
- **This file** — High-level view only. Check linked issues for current status and discussion.

## Contributing

See something you'd like to work on? Check the [Contributing Guide](CONTRIBUTING.md) and open an issue to discuss your approach before starting.