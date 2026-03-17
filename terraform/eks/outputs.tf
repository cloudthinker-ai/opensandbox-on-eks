output "cluster_endpoint" {
  description = "EKS cluster API endpoint"
  value       = local.cluster_endpoint
}

output "cluster_name" {
  description = "EKS cluster name (for aws eks update-kubeconfig)"
  value       = local.cluster_name
}

output "cluster_certificate_authority" {
  description = "EKS cluster CA certificate data (base64)"
  value       = local.cluster_ca
  sensitive   = true
}

output "ecr_repository_urls" {
  description = "Map of component name to ECR repository URL"
  value       = { for k, v in aws_ecr_repository.this : k => v.repository_url }
}

output "kubeconfig_command" {
  description = "Command to configure kubectl"
  value       = "aws eks update-kubeconfig --name ${local.cluster_name} --region ${var.region}"
}

output "helm_install_command" {
  description = "Helm install command for OpenSandbox"
  value       = <<-EOT
    helm install opensandbox ./charts/opensandbox \
      -f charts/opensandbox/values-eks.yaml \
      --set images.server.repository=${aws_ecr_repository.this["opensandbox/server"].repository_url} \
      --set images.controller.repository=${aws_ecr_repository.this["opensandbox/controller"].repository_url} \
      --set images.execd.repository=${aws_ecr_repository.this["opensandbox/execd"].repository_url} \
      --set images.egress.repository=${aws_ecr_repository.this["opensandbox/egress-sidecar"].repository_url}
  EOT
}

output "next_steps" {
  description = "Post-apply instructions"
  value       = <<-EOT

    ===== OpenSandbox EKS — Next Steps =====

    1. Configure kubectl:
       aws eks update-kubeconfig --name ${local.cluster_name} --region ${var.region}

    2. Create StorageClass and VolumeSnapshotClass:
       kubectl apply -f - <<YAML
    apiVersion: storage.k8s.io/v1
    kind: StorageClass
    metadata:
      name: gp3
    provisioner: ebs.csi.aws.com
    parameters:
      type: gp3
    volumeBindingMode: WaitForFirstConsumer
    allowVolumeExpansion: true
    ---
    apiVersion: snapshot.storage.k8s.io/v1
    kind: VolumeSnapshotClass
    metadata:
      name: ebs-vsc
    driver: ebs.csi.aws.com
    deletionPolicy: Delete
    YAML

    3. Build and push images to ECR:
       aws ecr get-login-password --region ${var.region} | docker login --username AWS --password-stdin ${local.account_id}.dkr.ecr.${var.region}.amazonaws.com

    4. Create imagePullSecret:
       kubectl create namespace opensandbox 2>/dev/null || true
       kubectl create secret docker-registry ecr-registry \
         --namespace opensandbox \
         --docker-server=${local.account_id}.dkr.ecr.${var.region}.amazonaws.com \
         --docker-username=AWS \
         --docker-password=$(aws ecr get-login-password --region ${var.region})

    5. Install with Helm:
       $(terraform output -raw helm_install_command)

    6. Verify:
       kubectl get pods -A | grep opensandbox

  EOT
}
