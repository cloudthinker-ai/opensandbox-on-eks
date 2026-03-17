# Deploy OpenSandbox on a New EKS Cluster

Green-field guide: creates a brand-new EKS cluster and deploys OpenSandbox from scratch.

## Prerequisites

| Tool | Minimum version | Purpose |
|------|----------------|---------|
| `eksctl` | 0.170+ | Cluster provisioning |
| `kubectl` | 1.29+ | Cluster management |
| `helm` | 3.14+ | Chart installation |
| `aws` CLI | v2 | IAM / ECR / addon management |
| Docker | 20.10+ | Building and pushing images |

## What this guide creates

### AWS resources

| Resource | Name / ID | Purpose |
|----------|-----------|---------|
| EKS cluster | `opensandbox` | Kubernetes control plane |
| Managed node group | Default (m6i.xlarge, 2–10 nodes) | Worker nodes |
| VPC + subnets | Auto-created by eksctl | Cluster networking |
| IAM OIDC provider | Linked to cluster | IRSA (IAM Roles for Service Accounts) |
| IAM role | `AmazonEKS_EBS_CSI_DriverRole` | EBS CSI driver permissions |
| ECR repositories | `opensandbox/server`, `controller`, `execd`, `egress-sidecar` | Container images |
| EBS volumes | One per sandbox (gp3, 20Gi default) | Sandbox persistent storage |
| EBS snapshots | One per paused sandbox | Sandbox archival |
| Load Balancer | Classic or NLB (optional) | Server endpoint |

### Kubernetes resources

| Resource | Scope | Purpose |
|----------|-------|---------|
| Namespace `opensandbox` | — | Server + sandbox pods |
| Namespace `opensandbox-system` | — | Controller |
| StorageClass `gp3` | Cluster-wide | EBS gp3 volumes |
| VolumeSnapshotClass `ebs-vsc` | Cluster-wide | EBS snapshots |
| CRD `batchsandboxes.sandbox.opensandbox.io` | Cluster-wide | Sandbox batch workloads |
| CRD `pools.sandbox.opensandbox.io` | Cluster-wide | Sandbox pool management |
| ClusterRole `opensandbox-server-role` | Cluster-wide | Server pod/exec/PVC/snapshot access |
| ClusterRole `opensandbox-manager-role` | Cluster-wide | Controller pod/BatchSandbox management |
| NetworkPolicy | Namespaced (`opensandbox`) | Sandbox egress restrictions |

---

## Step 1: Create the EKS Cluster

Each sandbox pod requests 1–2 vCPU and 2–4 Gi memory (see [architecture.md](../architecture/architecture.md#resource-profile)), so **m6i.xlarge** (4 vCPU / 16 Gi) fits roughly 3–4 sandboxes per node after system overhead.

```bash
eksctl create cluster \
  --name opensandbox \
  --region us-west-2 \
  --version 1.29 \
  --managed \
  --node-type m6i.xlarge \
  --nodes 2 \
  --nodes-min 2 \
  --nodes-max 10
```

Minimum 2 nodes recommended for high availability. This takes ~15–20 minutes.

## Step 2: OIDC Provider

Associate an OIDC provider for IRSA (IAM Roles for Service Accounts):

```bash
eksctl utils associate-iam-oidc-provider \
  --cluster opensandbox \
  --region us-west-2 \
  --approve
```

## Step 3: EBS CSI Driver

### 3a. Create the IAM role

```bash
eksctl create iamserviceaccount \
  --cluster opensandbox \
  --region us-west-2 \
  --namespace kube-system \
  --name ebs-csi-controller-sa \
  --role-name AmazonEKS_EBS_CSI_DriverRole \
  --attach-policy-arn arn:aws:iam::policy/service-role/AmazonEBSCSIDriverPolicy \
  --approve
```

### 3b. Install the addon

Replace `<ACCOUNT_ID>` with your AWS account ID.

```bash
eksctl create addon \
  --cluster opensandbox \
  --region us-west-2 \
  --name aws-ebs-csi-driver \
  --service-account-role-arn arn:aws:iam::<ACCOUNT_ID>:role/AmazonEKS_EBS_CSI_DriverRole
```

## Step 4: Snapshot Controller

The EBS CSI driver supports snapshots, but the Kubernetes snapshot CRDs and controller are **not** installed by default.

### Option A: EKS managed add-on (recommended)

```bash
eksctl create addon \
  --cluster opensandbox \
  --region us-west-2 \
  --name snapshot-controller
```

This installs the `VolumeSnapshot`, `VolumeSnapshotContent`, and `VolumeSnapshotClass` CRDs automatically.

### Option B: Self-managed (if you need a specific version)

```bash
SNAPSHOTTER_VERSION=v8.2.0

# Install CRDs
kubectl kustomize "https://github.com/kubernetes-csi/external-snapshotter/client/config/crd?ref=${SNAPSHOTTER_VERSION}" | kubectl create -f -

# Install snapshot controller into kube-system
kubectl -n kube-system kustomize "https://github.com/kubernetes-csi/external-snapshotter/deploy/kubernetes/snapshot-controller?ref=${SNAPSHOTTER_VERSION}" | kubectl create -f -
```

### Verify

```bash
kubectl get pods -n kube-system -l app.kubernetes.io/name=snapshot-controller
```

## Step 5: VPC CNI NetworkPolicy

Enable native NetworkPolicy enforcement in the VPC CNI:

```bash
aws eks update-addon \
  --cluster-name opensandbox \
  --region us-west-2 \
  --addon-name vpc-cni \
  --configuration-values '{"enableNetworkPolicy": "true"}'
```

Verify:

```bash
kubectl get ds -n kube-system aws-node -o jsonpath='{.spec.template.spec.containers[0].env}' | grep -o 'ENABLE_NETWORK_POLICY[^}]*'
```

## Step 6: Storage Classes

### gp3 StorageClass

```bash
kubectl apply -f - <<'EOF'
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: gp3
provisioner: ebs.csi.aws.com
parameters:
  type: gp3
volumeBindingMode: WaitForFirstConsumer
reclaimPolicy: Delete
allowVolumeExpansion: true
EOF
```

### VolumeSnapshotClass

```bash
kubectl apply -f - <<'EOF'
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshotClass
metadata:
  name: ebs-vsc
driver: ebs.csi.aws.com
deletionPolicy: Delete
EOF
```

## Step 7: Container Registry (ECR)

Replace `<ACCOUNT_ID>` and `<REGION>` with your values throughout this section.

### Create repositories and push images

```bash
# Authenticate Docker to ECR
aws ecr get-login-password --region <REGION> | \
  docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com

# Create repositories
for repo in opensandbox/server opensandbox/controller opensandbox/execd opensandbox/egress-sidecar; do
  aws ecr create-repository --repository-name "$repo" --region <REGION> || true
done

# Build images for linux/amd64 (from the repo root)
# --provenance=false --sbom=false prevents BuildKit attestation manifests that ECR rejects
docker build --platform linux/amd64 --provenance=false --sbom=false -t opensandbox/server:dev ./server
docker build --platform linux/amd64 --provenance=false --sbom=false -t opensandbox/controller:dev ./kubernetes
docker build --platform linux/amd64 --provenance=false --sbom=false -t opensandbox/execd:dev -f components/execd/Dockerfile .
docker build --platform linux/amd64 --provenance=false --sbom=false -t opensandbox/egress:dev -f components/egress/Dockerfile .

# Tag and push each image
for component in server controller execd; do
  docker tag "opensandbox/$component:dev" "<ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/opensandbox/$component:latest"
  docker push "<ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/opensandbox/$component:latest"
done

# Egress image has a different ECR repository name
docker tag "opensandbox/egress:dev" "<ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/opensandbox/egress-sidecar:latest"
docker push "<ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/opensandbox/egress-sidecar:latest"
```

> **Apple Silicon (ARM) Macs:** Docker Desktop on ARM may create attestation manifest lists even with `--provenance=false --sbom=false`. If `docker push` fails with "repository does not exist" errors referencing `*atest` repos, create the attestation repositories:
>
> ```bash
> for repo in opensandbox/server opensandbox/controller opensandbox/execd opensandbox/egress-sidecar; do
>   aws ecr create-repository --repository-name "${repo}atest" --region <REGION> || true
> done
> ```
>
> Then delete the old image tags and re-push. Alternatively, build on a native amd64 machine.

## Step 8: Helm Install

Edit [`values-eks-sample.yaml`](../../charts/opensandbox/values-eks-sample.yaml) — copy it to `values-eks.yaml`, replace all `<ACCOUNT_ID>` and `<REGION>` placeholders, and set `server.config.server.api_key`. The `values-eks.yaml` file is gitignored so your secrets stay safe.

```bash
cp charts/opensandbox/values-eks-sample.yaml charts/opensandbox/values-eks.yaml
# Edit charts/opensandbox/values-eks.yaml with your values
```

### Install

```bash
helm install opensandbox ./charts/opensandbox \
  -f charts/opensandbox/values-eks.yaml \
  --namespace opensandbox \
  --create-namespace
```

> **ECR authentication:** EKS nodes pull ECR images using their IAM role (`AmazonEC2ContainerRegistryReadOnly`), which is attached by default when using eksctl or our Terraform module. No imagePullSecret is needed for same-account ECR.
>
> For cross-account ECR or non-AWS registries, create a `docker-registry` secret manually and set `imagePullSecrets` in your values file.

## Step 9: Verify

```bash
# Pods running
kubectl get pods -n opensandbox
kubectl get pods -n opensandbox-system

# CRDs registered
kubectl get crd batchsandboxes.sandbox.opensandbox.io

# Server logs
kubectl logs -n opensandbox -l app.kubernetes.io/component=server --tail=20
```

Expected output: server and controller pods in `Running` state, CRDs present.

Or run the automated verification script (launches a temporary pod inside the cluster — no port-forward needed):

```bash
API_KEY="<your-api-key>" ./scripts/verify-eks-deployment.sh
```

This tests health, authentication, and a full sandbox lifecycle (create → run → pause → snapshot → resume → delete).

## Step 10: Create Your First Sandbox

The 4 images you built (server, controller, execd, egress-sidecar) are **infrastructure**. The sandbox itself runs any container image you specify in the API call — `python:3.11-slim`, `ubuntu:24.04`, `node:20`, or your own custom image. OpenSandbox injects the execd daemon automatically via init container.

### Port-forward the server

```bash
kubectl port-forward svc/opensandbox-server -n opensandbox 8080:8080 &
```

### Create a sandbox

```bash
curl -X POST "http://localhost:8080/v1/sandboxes" \
  -H "OPEN-SANDBOX-API-KEY: <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "image": { "uri": "python:3.11-slim" },
    "entrypoint": ["python", "-m", "http.server", "8000"],
    "timeout": 3600,
    "resourceLimits": { "cpu": "500m", "memory": "512Mi" }
  }' | jq .
```

### Verify the sandbox pod

```bash
kubectl get pods -n opensandbox -l opensandbox.io/component=sandbox
```

You should see a pod transition to `Running` within ~30 seconds.

### Delete the sandbox

```bash
curl -X DELETE -H "OPEN-SANDBOX-API-KEY: <your-api-key>" \
  "http://localhost:8080/v1/sandboxes/<sandbox-id>"
```

> **Private images:** If your sandbox image is in ECR or another private registry, pass `auth` in the image spec. See the [Getting Started guide](../getting-started.md) for details.
>
> **Custom images:** To build your own purpose-built sandbox image, see [Custom Images](../custom-images.md).

## Step 11: Expose the Server

### Option A: Load Balancer (recommended for production)

The `values-eks.yaml` sets `server.service.type: LoadBalancer`. By default EKS provisions a Classic Load Balancer. For an NLB, annotate the service:

```bash
kubectl annotate svc opensandbox-server -n opensandbox \
  service.beta.kubernetes.io/aws-load-balancer-type=external \
  service.beta.kubernetes.io/aws-load-balancer-nlb-target-type=ip \
  service.beta.kubernetes.io/aws-load-balancer-scheme=internet-facing
```

Get the external endpoint:

```bash
kubectl get svc opensandbox-server -n opensandbox \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'
```

For full ALB/NLB control, install the [AWS Load Balancer Controller](https://kubernetes-sigs.github.io/aws-load-balancer-controller/).

### Option B: Port-forward (testing only)

```bash
kubectl port-forward svc/opensandbox-server -n opensandbox 8080:8080
```

---

## Post-install

### NetworkPolicy tuning

The Helm chart creates a NetworkPolicy that blocks private CIDRs and `169.254.0.0/16` (AWS instance metadata) by default — see [`sandbox-networkpolicy.yaml`](../../charts/opensandbox/templates/sandbox-networkpolicy.yaml).

In `values-eks.yaml`, adjust `networkPolicy.clusterCIDRs` to match your actual VPC CIDR:

```yaml
networkPolicy:
  enabled: true
  clusterCIDRs:
    - 10.0.0.0/16    # Your VPC CIDR (narrowed from the default 10.0.0.0/8)
```

The default blocks all of `10.0.0.0/8` which is safe but overly broad. Narrowing it to your actual VPC CIDR prevents false positives if sandboxes need to reach external services in the `10.x.x.x` range.

### Snapshot configuration

`values-eks.yaml` enables the snapshot lifecycle by default:

```yaml
kubernetes:
  snapshot_enabled: true
  snapshot_class: "ebs-vsc"
  archive_after_seconds: 86400   # Archive PVC after 1 day of being paused
```

How the lifecycle works (see [architecture.md — Pause / Resume](../architecture/architecture.md#pause--resume-tiered-lifecycle)):

1. **Pause** — pod is deleted instantly, PVC retained, snapshot created in background
2. **Archive** — after `archive_after_seconds`, PVC is deleted (snapshot remains)
3. **Resume (warm)** — PVC still exists, pod mounts it (<5s)
4. **Resume (cold)** — PVC was archived, restored from EBS snapshot (~30–60s)

Tune `archive_after_seconds` based on your resume latency requirements:

- `3600` (1 hour) — aggressive archival, saves storage cost
- `86400` (1 day) — balanced default
- `604800` (7 days) — keep PVCs warm for a week

### Security

#### Pod Security Standards

Sandbox pods require **privileged** capabilities (`CAP_SYS_ADMIN`) for OverlayFS `mount` and `pivot_root`. The default `bootstrap.sh` drops privileges to `SANDBOX_USER` after setup. See [architecture.md — OverlayFS Persistence](../architecture/architecture.md#overlayfs-persistence).

If your cluster enforces Pod Security Standards at the namespace level, the sandbox namespace must allow `privileged`:

```bash
kubectl label namespace opensandbox \
  pod-security.kubernetes.io/enforce=privileged \
  pod-security.kubernetes.io/warn=privileged
```

#### API key authentication

Set a strong API key in your values:

```yaml
server:
  config:
    server:
      api_key: "<your-strong-api-key>"
```

All SDK clients must include this key in requests.

#### IRSA for Server (optional)

If the server needs AWS API access (e.g., S3 for artifacts), annotate the service account:

```bash
kubectl annotate serviceaccount opensandbox-server -n opensandbox \
  eks.amazonaws.com/role-arn=arn:aws:iam::<ACCOUNT_ID>:role/<SERVER_ROLE>
```

### Capacity planning

#### Per-sandbox resource consumption

| Component | CPU (idle) | CPU (active) | Memory |
|-----------|-----------|-------------|--------|
| Sandbox container | 50–100m | 0.5–4 vCPU | 512Mi–4Gi |
| execd daemon | ~10m | ~50m | 5–20 MB |
| Jupyter server | ~50m | 200m–1 vCPU | 100–300 MB |
| Egress sidecar | ~10m | ~50m | 20–50 MB |

**Recommended per sandbox:** 1–2 vCPU, 2–4 Gi memory.

#### Instance type recommendations

| Instance type | vCPU | Memory | Sandboxes per node (approx) |
|---------------|------|--------|---------------------------|
| m6i.xlarge | 4 | 16 Gi | 3–4 |
| m6i.2xlarge | 8 | 32 Gi | 6–8 |
| m6i.4xlarge | 16 | 64 Gi | 12–16 |

Reserve ~1 vCPU and 1–2 Gi per node for system pods (kubelet, kube-proxy, VPC CNI, CoreDNS).

#### Autoscaling

Use [Karpenter](https://karpenter.sh/) (recommended) or [Cluster Autoscaler](https://github.com/kubernetes/autoscaler/tree/master/cluster-autoscaler) to scale node groups based on pending sandbox pods.

---

## Troubleshooting

### PVC stuck in Pending

```bash
kubectl describe pvc <sandbox-id>-docker-data -n opensandbox
```

Common causes:

- **EBS CSI driver not installed** — check `kubectl get pods -n kube-system -l app.kubernetes.io/name=aws-ebs-csi-driver`
- **StorageClass not found** — ensure the `gp3` StorageClass exists: `kubectl get sc gp3`
- **Availability zone mismatch** — `WaitForFirstConsumer` binding mode avoids this; confirm it's set

### Snapshot creation fails

```bash
kubectl get volumesnapshot -n opensandbox
kubectl describe volumesnapshot <snapshot-name> -n opensandbox
```

Common causes:

- **Snapshot CRDs not installed** — `kubectl get crd volumesnapshots.snapshot.storage.k8s.io`
- **Snapshot controller not running** — `kubectl get pods -n kube-system -l app=snapshot-controller`
- **VolumeSnapshotClass missing** — `kubectl get volumesnapshotclass ebs-vsc`

### CrashLoopBackOff on sandbox pods

```bash
kubectl logs <pod-name> -n opensandbox -c sandbox
```

If the log shows `mount: permission denied` or `pivot_root: Operation not permitted`:

- The pod needs `CAP_SYS_ADMIN`. Ensure the namespace allows privileged pods (see [Security — Pod Security Standards](#pod-security-standards))
- Check that the container security context is not dropping `SYS_ADMIN`

### NetworkPolicy not enforced

- Confirm VPC CNI network policy is enabled (see [Step 5](#step-5-vpc-cni-networkpolicy))
- Verify the NetworkPolicy exists: `kubectl get networkpolicy -n opensandbox`
- Test with: `kubectl exec <sandbox-pod> -n opensandbox -- curl -sS --max-time 3 http://169.254.169.254/` — should time out

### ECR image pull errors

```bash
kubectl describe pod <pod-name> -n opensandbox | grep -A5 "Events:"
```

Common causes:

- **Node IAM role missing ECR policy** — eksctl attaches `AmazonEC2ContainerRegistryReadOnly` by default; verify with:
  ```bash
  NODE_ROLE=$(aws eks describe-nodegroup --cluster-name opensandbox --nodegroup-name <NODEGROUP> --region <REGION> --query 'nodegroup.nodeRole' --output text | awk -F/ '{print $NF}')
  aws iam list-attached-role-policies --role-name "$NODE_ROLE" | grep ContainerRegistry
  ```
- **Wrong region/account** — verify image URIs in values match your ECR repositories
- **Cross-account ECR** — for images in a different AWS account, create a `docker-registry` secret and set `imagePullSecrets` in your values file

---

## Clean up

Full teardown — removes everything created by this guide.

### 1. Uninstall the Helm release

```bash
helm uninstall opensandbox --namespace opensandbox
```

### 2. Delete CRDs (Helm preserves these by default)

```bash
kubectl delete crd batchsandboxes.sandbox.opensandbox.io pools.sandbox.opensandbox.io
```

### 3. Delete namespaces

```bash
kubectl delete namespace opensandbox opensandbox-system
```

### 4. Delete ECR repositories (optional)

```bash
for repo in opensandbox/server opensandbox/controller opensandbox/execd opensandbox/egress-sidecar; do
  aws ecr delete-repository --repository-name "$repo" --region <REGION> --force
done
```

### 5. Delete the EKS cluster

This deletes the cluster, node groups, VPC, and all associated AWS resources:

```bash
eksctl delete cluster --name opensandbox --region us-west-2
```
