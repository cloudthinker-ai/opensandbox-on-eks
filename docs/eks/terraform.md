# Deploy OpenSandbox on EKS with Terraform

End-to-end guide: from `terraform apply` to a running sandbox.

## What Terraform handles vs what you do

| Step | Done by | Status after `terraform apply` |
|------|---------|-------------------------------|
| VPC, subnets, IGW | Terraform | Done |
| EKS cluster + node group | Terraform | Done |
| IAM roles (cluster, node, EBS CSI) | Terraform | Done |
| OIDC provider (IRSA) | Terraform | Done |
| EBS CSI driver addon | Terraform | Done |
| Snapshot controller addon | Terraform | Done |
| VPC CNI NetworkPolicy | Terraform | Done |
| ECR repositories (4) | Terraform | Done |
| **Configure kubectl** | **You (Step 1)** | — |
| **Create StorageClass + VolumeSnapshotClass** | **You (Step 2)** | — |
| **Build & push images to ECR** | **You (Step 3)** | — |
| **Configure values-eks.yaml** | **You (Step 4)** | — |
| **Helm install** | **You (Step 5)** | — |
| **Verify & create first sandbox** | **You (Step 6)** | — |

---

## Prerequisites

- [Terraform](https://www.terraform.io/downloads) >= 1.5
- [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html) configured with credentials
- [Docker](https://docs.docker.com/get-docker/) >= 20.10
- [kubectl](https://kubernetes.io/docs/tasks/tools/) >= 1.29
- [Helm](https://helm.sh/docs/intro/install/) >= 3.14
- [jq](https://jqlang.github.io/jq/download/) (for parsing Terraform outputs)

---

## Phase 1: Provision Infrastructure (Terraform)

### New cluster

```bash
cd terraform/eks

# Configure
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars — set region, cluster_name, etc.

# Deploy (~15 min)
terraform init
terraform apply
```

### Existing cluster

Set `create_cluster = false` in `terraform.tfvars`:

```hcl
create_cluster = false
cluster_name   = "my-existing-cluster"
region         = "us-east-1"

# Skip addons already installed on your cluster
install_ebs_csi             = false
install_snapshot_controller = false
enable_network_policy       = false
```

```bash
terraform init
terraform apply
```

---

## Phase 2: Post-Apply Steps (Manual)

Run all commands from the **repository root** (not `terraform/eks`).

### Step 1: Configure kubectl

```bash
aws eks update-kubeconfig \
  --name $(cd terraform/eks && terraform output -raw cluster_name) \
  --region $(cd terraform/eks && terraform output -raw region 2>/dev/null || grep 'region' terraform/eks/terraform.tfvars | head -1 | cut -d'"' -f2)
```

Verify:

```bash
kubectl get nodes
```

You should see your node(s) in `Ready` state.

### Step 2: Create StorageClass and VolumeSnapshotClass

OpenSandbox uses gp3 EBS volumes for sandbox storage and EBS snapshots for pause/resume.

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
---
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshotClass
metadata:
  name: ebs-vsc
driver: ebs.csi.aws.com
deletionPolicy: Delete
EOF
```

### Step 3: Build and push images to ECR

```bash
# Set variables from Terraform outputs
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=$(cd terraform/eks && terraform output -raw cluster_endpoint | awk -F. '{print $(NF-3)}')
echo "ACCOUNT_ID=$ACCOUNT_ID  REGION=$REGION"

# Login to ECR
aws ecr get-login-password --region $REGION | \
  docker login --username AWS --password-stdin $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com

# Build images for linux/amd64 (from repo root)
# --provenance=false --sbom=false prevents BuildKit attestation manifests that ECR rejects
docker build --platform linux/amd64 --provenance=false --sbom=false -t opensandbox/server:dev ./server
docker build --platform linux/amd64 --provenance=false --sbom=false -t opensandbox/controller:dev ./kubernetes
docker build --platform linux/amd64 --provenance=false --sbom=false -t opensandbox/execd:dev -f components/execd/Dockerfile .
docker build --platform linux/amd64 --provenance=false --sbom=false -t opensandbox/egress:dev -f components/egress/Dockerfile .

# Tag and push
for component in server controller execd; do
  ECR_URL=$(cd terraform/eks && terraform output -json ecr_repository_urls | jq -r ".\"opensandbox/$component\"")
  docker tag "opensandbox/$component:dev" "$ECR_URL:latest"
  docker push "$ECR_URL:latest"
done

# Egress has a different local name vs ECR name
ECR_URL=$(cd terraform/eks && terraform output -json ecr_repository_urls | jq -r '."opensandbox/egress-sidecar"')
docker tag "opensandbox/egress:dev" "$ECR_URL:latest"
docker push "$ECR_URL:latest"
```

> **Apple Silicon (ARM) Macs:** Docker Desktop on ARM may create attestation manifest lists even with `--provenance=false --sbom=false`. If `docker push` fails with "repository does not exist" errors referencing `*atest` repos, create the attestation repositories:
>
> ```bash
> for repo in opensandbox/server opensandbox/controller opensandbox/execd opensandbox/egress-sidecar; do
>   aws ecr create-repository --repository-name "${repo}atest" --region $REGION || true
> done
> ```
>
> Then delete the old image tags and re-push. Alternatively, build on a native amd64 machine.

### Step 4: Configure Helm values

Create your values file from the sample template. `values-eks.yaml` is gitignored so your secrets won't leak; `values-eks-sample.yaml` is the committed template with placeholders.

```bash
# Create your values file from the sample (gitignored)
cp charts/opensandbox/values-eks-sample.yaml charts/opensandbox/values-eks.yaml

# Replace placeholders with your real values
sed -i.bak \
  -e "s/<ACCOUNT_ID>/$ACCOUNT_ID/g" \
  -e "s/<REGION>/$REGION/g" \
  charts/opensandbox/values-eks.yaml
rm charts/opensandbox/values-eks.yaml.bak

# Set an API key — edit charts/opensandbox/values-eks.yaml:
#   server.config.server.api_key: "your-strong-api-key-here"
```

> **Important:** Edit `values-eks.yaml` and set `server.config.server.api_key` to a strong secret. All API and SDK calls require this key.
>
> **Never commit `values-eks.yaml`** — it's gitignored. Only `values-eks-sample.yaml` (with placeholders) is tracked in git.

### Step 5: Helm install

```bash
helm install opensandbox ./charts/opensandbox \
  -f charts/opensandbox/values-eks.yaml \
  --namespace opensandbox \
  --create-namespace
```

The chart automatically creates the `opensandbox` and `opensandbox-system` namespaces. If you pre-created them, set `createNamespaces: false` in your values file.

> **ECR authentication:** EKS nodes pull ECR images using their IAM role (`AmazonEC2ContainerRegistryReadOnly`), which is attached by default when using eksctl or our Terraform module. No imagePullSecret is needed for same-account ECR.
>
> For cross-account ECR or non-AWS registries, create a `docker-registry` secret manually and set `imagePullSecrets` in your values file.

### Step 6: Verify

```bash
# Check pods (wait 1-2 minutes for everything to start)
kubectl get pods -n opensandbox
kubectl get pods -n opensandbox-system

# Check CRDs
kubectl get crd batchsandboxes.sandbox.opensandbox.io

# Server logs
kubectl logs -n opensandbox -l app.kubernetes.io/component=server --tail=20
```

Expected: server and controller pods in `Running` state.

Or run the automated verification script (launches a temporary pod inside the cluster — no port-forward needed):

```bash
API_KEY="<your-api-key>" ./scripts/verify-eks-deployment.sh
```

This tests health, authentication, and a full sandbox lifecycle (create → run → pause → snapshot → resume → delete).

---

## Phase 3: Create Your First Sandbox

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

### Check status

```bash
curl -H "OPEN-SANDBOX-API-KEY: <your-api-key>" \
  "http://localhost:8080/v1/sandboxes/<sandbox-id>" | jq .status
```

Wait until `state` is `Running`.

### Access the running service

The sandbox is running a Python HTTP server on port 8000. Get its public URL:

```bash
curl -s -H "OPEN-SANDBOX-API-KEY: <your-api-key>" \
  "http://localhost:8080/v1/sandboxes/<sandbox-id>/endpoints/8000" | jq .
```

Access the service using the returned endpoint:

```bash
curl http://<sandbox-id>-8000.sandbox.example.com/
```

For more details, see the [Port Forwarding Guide](../port-forwarding.md).

### Delete the sandbox

```bash
curl -X DELETE -H "OPEN-SANDBOX-API-KEY: <your-api-key>" \
  "http://localhost:8080/v1/sandboxes/<sandbox-id>"
```

---

## Scaling for Production

Update `terraform.tfvars` and run `terraform apply`:

```hcl
node_instance_type = "m6i.xlarge"  # 4 vCPU, 16 Gi — fits 3-4 sandboxes/node
node_min_size      = 2             # HA: minimum 2 nodes
node_max_size      = 10
node_desired_size  = 2
```

### Instance Type Reference

| Instance | vCPU | Memory | Sandboxes/node | On-demand $/hr | Use case |
|----------|------|--------|---------------|----------------|----------|
| t3.small | 2 | 2 Gi | 0-1 | ~$0.02 | Testing only |
| t3.medium | 2 | 4 Gi | 1 | ~$0.04 | Dev |
| t3.large | 2 | 8 Gi | 1-2 | ~$0.08 | Staging |
| m6i.xlarge | 4 | 16 Gi | 3-4 | ~$0.19 | Production |
| m6i.2xlarge | 8 | 32 Gi | 6-8 | ~$0.38 | Production (high density) |

Each sandbox requires approximately 1-2 vCPU and 2-4 Gi memory.

---

## Variables Reference

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `aws_profile` | string | `null` | AWS CLI profile name |
| `create_cluster` | bool | `true` | `true` = new cluster, `false` = existing |
| `cluster_name` | string | `"opensandbox"` | EKS cluster name |
| `region` | string | `"us-east-1"` | AWS region |
| `cluster_version` | string | `"1.29"` | EKS Kubernetes version |
| `node_instance_type` | string | `"t3.small"` | EC2 instance type for node group |
| `node_min_size` | number | `1` | Minimum nodes |
| `node_max_size` | number | `2` | Maximum nodes |
| `node_desired_size` | number | `1` | Desired nodes |
| `vpc_cidr` | string | `"10.0.0.0/16"` | VPC CIDR (new cluster only) |
| `install_ebs_csi` | bool | `true` | Install EBS CSI driver addon |
| `install_snapshot_controller` | bool | `true` | Install snapshot controller addon |
| `enable_network_policy` | bool | `true` | Enable VPC CNI NetworkPolicy |

## Outputs Reference

| Output | Description |
|--------|-------------|
| `cluster_endpoint` | EKS API server endpoint |
| `cluster_name` | Cluster name for `aws eks update-kubeconfig` |
| `cluster_certificate_authority` | Base64-encoded CA cert (sensitive) |
| `ecr_repository_urls` | Map of component name to ECR URL |
| `kubeconfig_command` | Ready-to-run kubeconfig command |
| `helm_install_command` | Ready-to-run Helm install command |
| `next_steps` | Full post-apply instructions |

---

## Tear Down

```bash
# 1. Remove Helm release
helm uninstall opensandbox --namespace opensandbox

# 2. Delete Kubernetes resources not managed by Helm
kubectl delete storageclass gp3
kubectl delete volumesnapshotclass ebs-vsc
kubectl delete namespace opensandbox opensandbox-system

# 3. Destroy AWS infrastructure
cd terraform/eks
terraform destroy
```

> **Note:** ECR repositories are created with `force_delete = false`. If they contain images, you must delete the images manually or set `force_delete = true` in `ecr.tf` before destroying.
