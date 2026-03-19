# OpenSandbox Helm Chart

![Version: 0.1.0](https://img.shields.io/badge/Version-0.1.0-informational?style=flat-square)
![AppVersion: 0.1.0](https://img.shields.io/badge/AppVersion-0.1.0-informational?style=flat-square)
![Type: application](https://img.shields.io/badge/Type-application-informational?style=flat-square)

AI sandbox platform for isolated code execution on Kubernetes.

## Prerequisites

- Kubernetes >= 1.26
- Helm >= 3.12
- A CNI plugin that enforces NetworkPolicy (e.g. Calico, Cilium) if `networkPolicy.enabled=true`

## Quick Start

```bash
helm install opensandbox ./charts/opensandbox \
  -f charts/opensandbox/values-eks-sample.yaml \
  --set server.config.server.api_key="<your-api-key>"
```

## Configuration

The following tables list the configurable parameters and their default values.

### Global

| Parameter          | Description                                            | Default |
| ------------------ | ------------------------------------------------------ | ------- |
| `nameOverride`      | Override the chart name                                | `""`    |
| `fullnameOverride`  | Override the full release name                         | `""`    |
| `createNamespaces`  | Create namespaces automatically                        | `true`  |
| `crds.install`      | Install CRDs (BatchSandbox, etc.)                      | `true`  |
| `imagePullSecrets`  | List of image pull secret names for private registries | `[]`    |

### Images

| Parameter                      | Description                     | Default                      |
| ------------------------------ | ------------------------------- | ---------------------------- |
| `images.server.repository`     | Server image repository         | `opensandbox/server`         |
| `images.server.tag`            | Server image tag                | `dev`                        |
| `images.server.pullPolicy`     | Server image pull policy        | `IfNotPresent`               |
| `images.controller.repository` | Controller image repository     | `opensandbox/controller`     |
| `images.controller.tag`        | Controller image tag            | `dev`                        |
| `images.controller.pullPolicy` | Controller image pull policy    | `IfNotPresent`               |
| `images.execd.repository`      | Execd sidecar image repository  | `opensandbox/execd`          |
| `images.execd.tag`             | Execd sidecar image tag         | `dev`                        |
| `images.egress.repository`     | Egress sidecar image repository | `opensandbox/egress-sidecar` |
| `images.egress.tag`            | Egress sidecar image tag        | `v1.0.1`                     |

### Controller

| Parameter                              | Description                                           | Default |
| -------------------------------------- | ----------------------------------------------------- | ------- |
| `controller.replicas`                  | Number of controller replicas                         | `1`     |
| `controller.namespace`                 | Controller namespace (defaults to `<release>-system`) | `""`    |
| `controller.leaderElect`               | Enable leader election for HA                         | `true`  |
| `controller.resources.requests.cpu`    | CPU request                                           | `10m`   |
| `controller.resources.requests.memory` | Memory request                                        | `64Mi`  |
| `controller.resources.limits.cpu`      | CPU limit                                             | `500m`  |
| `controller.resources.limits.memory`   | Memory limit                                          | `128Mi` |
| `controller.tolerations`               | Tolerations for controller pods                       | `[]`    |

### Server

| Parameter                          | Description                                | Default     |
| ---------------------------------- | ------------------------------------------ | ----------- |
| `server.replicas`                  | Number of server replicas                  | `1`         |
| `server.namespace`                 | Server namespace (defaults to `<release>`) | `""`        |
| `server.resources.requests.cpu`    | CPU request                                | `200m`      |
| `server.resources.requests.memory` | Memory request                             | `256Mi`     |
| `server.resources.limits.cpu`      | CPU limit                                  | `1`         |
| `server.resources.limits.memory`   | Memory limit                               | `512Mi`     |
| `server.tolerations`               | Tolerations for server pods                | `[]`        |
| `server.service.type`              | Kubernetes Service type                    | `ClusterIP` |
| `server.service.port`              | Service port                               | `8080`      |
| `server.service.nodePort`          | NodePort (only when type is `NodePort`)    | `null`      |

### Server Config

These values are rendered into `config.toml` inside the server ConfigMap.

| Parameter                                                 | Description                                           | Default                          |
| --------------------------------------------------------- | ----------------------------------------------------- | -------------------------------- |
| `server.config.server.host`                               | Listen address                                        | `0.0.0.0`                        |
| `server.config.server.port`                               | Listen port                                           | `8080`                           |
| `server.config.server.log_level`                          | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`)       | `INFO`                           |
| `server.config.server.api_key`                            | API key for authentication (empty = disabled)         | `""`                             |
| `server.config.runtime.type`                              | Runtime type                                          | `kubernetes`                     |
| `server.config.runtime.startupSweepTimeout`               | Startup sweep timeout (seconds)                       | `5`                              |
| `server.config.kubernetes.namespace`                      | Sandbox namespace (auto-filled from server namespace) | `""`                             |
| `server.config.kubernetes.informer_enabled`               | Enable the Kubernetes informer                        | `true`                           |
| `server.config.kubernetes.informer_resync_seconds`        | Informer resync interval                              | `300`                            |
| `server.config.kubernetes.informer_watch_timeout_seconds` | Informer watch timeout                                | `60`                             |
| `server.config.kubernetes.workload_provider`              | Workload provider type                                | `batchsandbox`                   |
| `server.config.kubernetes.filesystem_persistence`         | Enable filesystem persistence via PVCs                | `true`                           |
| `server.config.kubernetes.snapshot_enabled`               | Enable VolumeSnapshot support                         | `false`                          |
| `server.config.kubernetes.snapshot_class`                 | VolumeSnapshotClass name                              | `""`                             |
| `server.config.kubernetes.snapshot_max_retention`         | Maximum snapshots to retain per sandbox               | `1`                              |
| `server.config.kubernetes.archive_after_seconds`          | Archive sandbox after this idle time                  | `3600`                           |
| `server.config.kubernetes.storage_class`                  | StorageClass for sandbox PVCs                         | `""`                             |
| `server.config.kubernetes.storage_size`                   | PVC size per sandbox                                  | `20Gi`                           |
| `server.config.ingress.mode`                              | Ingress mode                                          | `direct`                         |
| `server.config.storage.allowed_host_paths`                | Allowed host paths for volume mounts                  | `[]`                             |
| `server.config.egress.enabled`                            | Enable egress sidecar injection                       | `true`                           |
| `server.config.egress.image`                              | Egress sidecar image                                  | `opensandbox/egress-sidecar:dev` |
| `server.config.egress.upstream_dns`                       | Upstream DNS server for egress sidecar                | `""`                             |

### BatchSandbox Template

| Parameter                     | Description                             | Default             |
| ----------------------------- | --------------------------------------- | ------------------- |
| `server.batchsandboxTemplate` | YAML template for BatchSandbox pod spec | _(see values.yaml)_ |

The template defines the pod spec used when creating sandbox workloads. Override it to customize container images, volumes, resource limits, or tolerations for sandbox pods.

### Network Access Config

| Parameter                                    | Description                               | Default             |
| -------------------------------------------- | ----------------------------------------- | ------------------- |
| `networkAccessConfig.enabled`                | Enable per-sandbox network access control | `false`             |
| `networkAccessConfig.policies`               | List of network access policies           | _(see values.yaml)_ |

> **Note:** Network access control requires both `server.config.egress.enabled=true` (for sidecar injection) and `networkPolicy.enabled=true` (for Kubernetes-level enforcement). Enabling only one results in partial enforcement.

### Network Policy

| Parameter                    | Description                               | Default                                       |
| ---------------------------- | ----------------------------------------- | --------------------------------------------- |
| `networkPolicy.enabled`      | Create NetworkPolicy for sandbox pods     | `true`                                        |
| `networkPolicy.clusterCIDRs` | Private CIDRs to block outbound access to | `[10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16]` |
| `networkPolicy.dnsNamespace` | Namespace of the DNS service              | `kube-system`                                 |
| `networkPolicy.dnsSelector`  | Label selector for DNS pods               | `{k8s-app: kube-dns}`                         |
| `networkPolicy.execdPort`    | Port used by the execd sidecar            | `44772`                                       |

## Production Deployment

Use the provided `values-eks-sample.yaml` overlay for production:

```bash
helm install opensandbox ./charts/opensandbox \
  -f charts/opensandbox/values-eks-sample.yaml
```

Key things to change in your production values:

- **Container registry** — Replace `registry.example.com` with your actual registry
- **API key** — Set `server.config.server.api_key` to a strong secret
- **Storage class** — Set `server.config.kubernetes.storage_class` to your cluster's StorageClass (e.g. `gp3`)
- **Snapshot class** — Set `server.config.kubernetes.snapshot_class` if using VolumeSnapshots (e.g. `ebs-snapshot-class`)
- **Image pull secrets** — Configure `imagePullSecrets` if using a private registry
- **Replicas** — Production defaults to 2 replicas each for server and controller

## Uninstalling

```bash
helm uninstall opensandbox
```

> **Note:** CRDs are not removed by `helm uninstall`. To fully clean up, delete them manually:
>
> ```bash
> kubectl delete crd batchsandboxes.opensandbox.io
> ```
