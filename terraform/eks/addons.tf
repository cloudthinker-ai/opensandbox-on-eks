# =============================================================================
# EKS Addons — each gated by its own toggle variable
# =============================================================================

resource "aws_eks_addon" "ebs_csi" {
  count = var.install_ebs_csi ? 1 : 0

  cluster_name             = local.cluster_name
  addon_name               = "aws-ebs-csi-driver"
  service_account_role_arn = aws_iam_role.ebs_csi[0].arn

  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  depends_on = [
    aws_eks_node_group.this,
    aws_iam_role_policy_attachment.ebs_csi,
  ]

  tags = local.common_tags
}

resource "aws_eks_addon" "snapshot_controller" {
  count = var.install_snapshot_controller ? 1 : 0

  cluster_name = local.cluster_name
  addon_name   = "snapshot-controller"

  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  depends_on = [
    aws_eks_node_group.this,
  ]

  tags = local.common_tags
}

resource "aws_eks_addon" "vpc_cni" {
  count = var.enable_network_policy ? 1 : 0

  cluster_name = local.cluster_name
  addon_name   = "vpc-cni"

  configuration_values = jsonencode({
    enableNetworkPolicy = "true"
  })

  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  depends_on = [
    aws_eks_node_group.this,
  ]

  tags = local.common_tags
}
