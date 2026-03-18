# Deploy OpenSandbox on an Existing EKS Cluster

Brown-field guide: adds OpenSandbox to a running EKS cluster. Focuses on what to check before deploying and what gets added to your cluster.

## Prerequisites

| Tool | Minimum version | Purpose |
|------|----------------|---------|
| `kubectl` | 1.29+ | Cluster management (configured for your cluster) |
| `helm` | 3.14+ | Chart installation |
| `aws` CLI | v2 | ECR / addon management |
| Docker | 20.10+ | Building and pushing images |

**Existing cluster requirements:**

- EKS version 1.29 or later
- `kubectl` access with permissions to create namespaces, CRDs, and ClusterRoles
- Sufficient node capacity (see [Preflight checklist](#preflight-checklist))

## What this guide creates

### Kubernetes resources

| Resource | Scope | Purpose |
|----------|-------|---------|
| Namespace `opensandbox` | — | Server + sandbox pods |
| Namespace `opensandbox-system` | — | Controller |
| StorageClass `gp3` | Cluster-wide | EBS gp3 volumes (skip if you already have one) |
| VolumeSnapshotClass `ebs-vsc` | Cluster-wide | EBS snapshots (skip if you already have one) |
| CRD `batchsandboxes.sandbox.opensandbox.io` | Cluster-wide | Sandbox batch workloads |
| CRD `pools.sandbox.opensandbox.io` | Cluster-wide | Sandbox pool management |
| ClusterRole `opensandbox-server-role` | Cluster-wide | Server permissions (see [RBAC impact](#rbac-impact)) |
| ClusterRole `opensandbox-manager-role` | Cluster-wide | Controller permissions (see [RBAC impact](#rbac-impact)) |
| NetworkPolicy | Namespaced (`opensandbox`) | Sandbox egress restrictions |

### AWS resources

| Resource | Purpose |
|----------|---------|
| ECR repositories (4) | Container images |
| EBS volumes (gp3) | One per sandbox, dynamically provisioned |
| EBS snapshots | One per paused/archived sandbox |

## What this guide does NOT modify

- **Existing namespaces** — no changes to any namespace other than `opensandbox` and `opensandbox-system`
- **Existing node groups** — no node group modifications; uses your existing nodes
- **Existing RBAC** — no modifications to existing Roles, ClusterRoles, or bindings
- **VPC or subnets** — no network infrastructure changes
- **Existing addons** — step 1 only installs addons if missing; never reconfigures existing ones
- **kube-system** — no pods deployed into kube-system (addon installation is via EKS managed addons)
- **Cluster-level settings** — no changes to API server, authentication, or authorization config

## RBAC impact

The Helm chart creates **2 ClusterRoles** with **cluster-wide** scope. Both are bound only to OpenSandbox service accounts.

### `opensandbox-server-role`

Bound to: `opensandbox-server` service account in `opensandbox` namespace.

| API Group | Resources | Verbs |
|-----------|-----------|-------|
| `""` (core) | pods, pods/log, pods/status, pods/exec, services, events | get, list, watch, create, update, patch, delete |
| `""` (core) | namespaces | get, list |
| `""` (core) | persistentvolumeclaims | get, list, create, delete |
| `""` (core) | secrets | get, create, delete |
| `snapshot.storage.k8s.io` | volumesnapshots | get, list, create, delete |
| `sandbox.opensandbox.io` | batchsandboxes, batchsandboxes/status, pools, pools/status | get, list, watch, create, update, patch, delete |

**Why cluster-wide?** The server needs `pods/exec` to attach to sandbox pods. Kubernetes does not support namespace-scoped exec permissions — it is an all-or-nothing cluster-wide permission. In practice, the server only targets pods in the `opensandbox` namespace.

### `opensandbox-manager-role`

Bound to: `opensandbox-controller-manager` service account in `opensandbox-system` namespace.

| API Group | Resources | Verbs |
|-----------|-----------|-------|
| `""` (core) | events, pods | get, list, watch, create, update, patch, delete |
| `""` (core) | pods/status | get, patch, update |
| `sandbox.opensandbox.io` | batchsandboxes, pools | get, list, watch, create, update, patch, delete |
| `sandbox.opensandbox.io` | batchsandboxes/finalizers, pools/finalizers | update |
| `sandbox.opensandbox.io` | batchsandboxes/status, pools/status | get, patch, update |

The controller also creates a **namespaced Role** for leader election (configmaps + leases) scoped to `opensandbox-system` only.

---

## Preflight checklist

Run these checks before deploying. Every item should pass.

### EKS version

```bash
kubectl version --short 2>/dev/null | grep Server
# Must be >= 1.29
```

### EBS CSI driver installed

```bash
kubectl get pods -n kube-system -l app.kubernetes.io/name=aws-ebs-csi-driver
# Should show running pods. If empty → install in Step 1.
```

### Snapshot controller installed

```bash
kubectl get pods -n kube-system -l app.kubernetes.io/name=snapshot-controller
# Should show running pods. If empty → install in Step 1.
```

### VPC CNI NetworkPolicy enabled

```bash
kubectl get ds -n kube-system aws-node -o jsonpath='{.spec.template.spec.containers[0].env}' | grep -o 'ENABLE_NETWORK_POLICY[^}]*'
# Should show ENABLE_NETWORK_POLICY with value "true". If not → enable in Step 1.
```

### No conflicting namespaces

```bash
kubectl get namespace opensandbox opensandbox-system 2>&1
# Should return "not found" for both. If they exist, choose different names or clean up first.
```

### No conflicting CRDs

```bash
kubectl get crd batchsandboxes.sandbox.opensandbox.io 2>&1
# Should return "not found". If it exists, a previous installation may need cleanup.
```

### Existing gp3 StorageClass?

```bash
kubectl get sc gp3
# If this exists, you can skip Step 2's StorageClass creation.
```

### Node capacity

```bash
kubectl top nodes
# Ensure at least 1–2 vCPU and 2–4 Gi memory available per sandbox you plan to run.
# Each m6i.xlarge node fits ~3–4 sandboxes after system overhead.
```

---

## Step 1: Install missing addons (if needed)

Skip any addon that is already installed (per the preflight checklist above).

### EBS CSI driver

```bash
# Check if already installed
kubectl get pods -n kube-system -l app.kubernetes.io/name=aws-ebs-csi-driver
# If pods are running, skip this section.

# Ensure OIDC provider is associated
eksctl utils associate-iam-oidc-provider \
  --cluster <CLUSTER_NAME> \
  --region <REGION> \
  --approve

# Create IAM role
eksctl create iamserviceaccount \
  --cluster <CLUSTER_NAME> \
  --region <REGION> \
  --namespace kube-system \
  --name ebs-csi-controller-sa \
  --role-name AmazonEKS_EBS_CSI_DriverRole \
  --attach-policy-arn arn:aws:iam::policy/service-role/AmazonEBSCSIDriverPolicy \
  --approve

# Install addon
eksctl create addon \
  --cluster <CLUSTER_NAME> \
  --region <REGION> \
  --name aws-ebs-csi-driver \
  --service-account-role-arn arn:aws:iam::<ACCOUNT_ID>:role/AmazonEKS_EBS_CSI_DriverRole
```

### Snapshot controller

```bash
# Check if already installed
kubectl get pods -n kube-system -l app.kubernetes.io/name=snapshot-controller
# If pods are running, skip this section.

eksctl create addon \
  --cluster <CLUSTER_NAME> \
  --region <REGION> \
  --name snapshot-controller
```

### VPC CNI NetworkPolicy

```bash
# Check if already enabled
kubectl get ds -n kube-system aws-node -o jsonpath='{.spec.template.spec.containers[0].env}' | grep -o 'ENABLE_NETWORK_POLICY[^}]*'
# If value is "true", skip this section.

aws eks update-addon \
  --cluster-name <CLUSTER_NAME> \
  --region <REGION> \
  --addon-name vpc-cni \
  --configuration-values '{"enableNetworkPolicy": "true"}'
```

## Step 2: Storage Classes (if needed)

### gp3 StorageClass

```bash
# Check if already exists
kubectl get sc gp3
# If it exists, skip this.

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
# Check if already exists
kubectl get volumesnapshotclass ebs-vsc
# If it exists, skip this.

kubectl apply -f - <<'EOF'
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshotClass
metadata:
  name: ebs-vsc
driver: ebs.csi.aws.com
deletionPolicy: Delete
EOF
```

## Step 3: Container Registry (ECR)

Replace `<ACCOUNT_ID>` and `<REGION>` with your values throughout this section.

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

## Step 4: Helm Install

Edit [`values-eks-sample.yaml`](../../charts/opensandbox/values-eks-sample.yaml) — copy it to `values-eks.yaml`, replace all `<ACCOUNT_ID>` and `<REGION>` placeholders, and set `server.config.server.api_key`. The `values-eks.yaml` file is gitignored so your secrets stay safe.

**Important:** Update `networkPolicy.clusterCIDRs` to match your actual VPC CIDR. The default `10.0.0.0/8` is safe but overly broad:

```yaml
networkPolicy:
  enabled: true
  clusterCIDRs:
    - 10.0.0.0/16    # Replace with your actual VPC CIDR
```

Find your VPC CIDR:

```bash
aws eks describe-cluster --name <CLUSTER_NAME> --region <REGION> \
  --query 'cluster.resourcesVpcConfig.vpcId' --output text | \
  xargs -I{} aws ec2 describe-vpcs --vpc-ids {} --region <REGION> \
  --query 'Vpcs[0].CidrBlock' --output text
```

### Verify node IAM role has ECR access

Unlike eksctl or our Terraform module (which attach `AmazonEC2ContainerRegistryReadOnly` automatically), existing clusters may not have this policy. Verify:

```bash
# Find your node group's IAM role
NODE_ROLE=$(aws eks describe-nodegroup \
  --cluster-name <CLUSTER_NAME> \
  --nodegroup-name <NODEGROUP_NAME> \
  --region <REGION> \
  --query 'nodegroup.nodeRole' --output text | awk -F/ '{print $NF}')

# Check for ECR policy
aws iam list-attached-role-policies --role-name "$NODE_ROLE" | grep ContainerRegistry
```

If not attached, add it:

```bash
aws iam attach-role-policy \
  --role-name "$NODE_ROLE" \
  --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly
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

## Step 5: Pod Security Labels

Sandbox pods require `CAP_SYS_ADMIN` for OverlayFS. If your cluster enforces Pod Security Standards, label the namespace:

```bash
kubectl label namespace opensandbox \
  pod-security.kubernetes.io/enforce=privileged \
  pod-security.kubernetes.io/warn=privileged
```

## Step 6: Verify

### Check OpenSandbox pods

```bash
kubectl get pods -n opensandbox
kubectl get pods -n opensandbox-system
```

Expected: server and controller pods in `Running` state.

Or run the automated verification script (launches a temporary pod inside the cluster — no port-forward needed):

```bash
API_KEY="<your-api-key>" ./scripts/verify-eks-deployment.sh
```

This tests health, authentication, and a full sandbox lifecycle (create → run → pause → snapshot → resume → delete).

### Check CRDs

```bash
kubectl get crd batchsandboxes.sandbox.opensandbox.io
```

### Check server logs

```bash
kubectl logs -n opensandbox -l app.kubernetes.io/component=server --tail=20
```

### Verify no impact on other namespaces

```bash
kubectl get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded
# Should not show any new failures in existing namespaces.
```

## Step 7: Create Your First Sandbox

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

## Step 8: Expose the Server

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

## Troubleshooting

### PVC stuck in Pending

```bash
kubectl describe pvc <sandbox-id>-docker-data -n opensandbox
```

Common causes:

- **EBS CSI driver not installed** — check `kubectl get pods -n kube-system -l app.kubernetes.io/name=aws-ebs-csi-driver`
- **StorageClass not found** — ensure the `gp3` StorageClass exists: `kubectl get sc gp3`
- **StorageClass name conflict** — if your cluster uses a different name for gp3, update `server.config.kubernetes.storage_class` in values
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

- The pod needs `CAP_SYS_ADMIN`. Ensure the namespace allows privileged pods (see [Step 5](#step-5-pod-security-labels))
- Check that the container security context is not dropping `SYS_ADMIN`

### NetworkPolicy not enforced

- Confirm VPC CNI network policy is enabled (see [Step 1](#vpc-cni-networkpolicy))
- Verify the NetworkPolicy exists: `kubectl get networkpolicy -n opensandbox`
- Test with: `kubectl exec <sandbox-pod> -n opensandbox -- curl -sS --max-time 3 http://169.254.169.254/` — should time out

### ECR image pull errors

```bash
kubectl describe pod <pod-name> -n opensandbox | grep -A5 "Events:"
```

Common causes:

- **Node IAM role missing ECR policy** — verify with:
  ```bash
  NODE_ROLE=$(aws eks describe-nodegroup --cluster-name <CLUSTER_NAME> --nodegroup-name <NODEGROUP> --region <REGION> --query 'nodegroup.nodeRole' --output text | awk -F/ '{print $NF}')
  aws iam list-attached-role-policies --role-name "$NODE_ROLE" | grep ContainerRegistry
  ```
  If missing, attach `AmazonEC2ContainerRegistryReadOnly` (see [Step 4 — Verify node IAM role](#verify-node-iam-role-has-ecr-access)).
- **Wrong region/account** — verify image URIs in values match your ECR repositories
- **Cross-account ECR** — for images in a different AWS account, create a `docker-registry` secret and set `imagePullSecrets` in your values file

### RBAC permission issues

If the server or controller pods show RBAC errors in logs:

```bash
kubectl logs -n opensandbox -l app.kubernetes.io/component=server --tail=50 | grep -i "forbidden\|unauthorized"
kubectl logs -n opensandbox-system -l app.kubernetes.io/component=controller --tail=50 | grep -i "forbidden\|unauthorized"
```

Common causes:

- **ClusterRole not created** — `kubectl get clusterrole | grep opensandbox`
- **ClusterRoleBinding misconfigured** — `kubectl get clusterrolebinding | grep opensandbox`
- **Admission controller blocking** — check if a policy engine (OPA/Gatekeeper, Kyverno) is rejecting the ClusterRole

### CRD conflicts

If CRDs already exist from a previous installation:

```bash
kubectl get crd batchsandboxes.sandbox.opensandbox.io -o jsonpath='{.metadata.annotations}'
```

If the CRD is owned by a different Helm release, delete it first:

```bash
kubectl delete crd batchsandboxes.sandbox.opensandbox.io pools.sandbox.opensandbox.io
```

Then re-run `helm install`.

---

## Uninstall

### 1. Uninstall the Helm release

```bash
helm uninstall opensandbox --namespace opensandbox
```

### 2. Delete CRDs

Helm preserves CRDs by default (`helm.sh/resource-policy: keep`). Remove them manually:

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

### 5. Remove StorageClass and VolumeSnapshotClass (if created by this guide)

Only remove these if no other workloads use them:

```bash
kubectl delete sc gp3
kubectl delete volumesnapshotclass ebs-vsc
```

> **This will NOT affect other workloads.** OpenSandbox is fully contained within its own namespaces, CRDs, and service accounts. Uninstalling removes only OpenSandbox resources.
