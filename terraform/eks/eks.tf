# =============================================================================
# EKS Cluster — create new or reference existing
# =============================================================================

# --- New cluster ---

resource "aws_eks_cluster" "this" {
  count = var.create_cluster ? 1 : 0

  name     = local.cluster_name
  version  = var.cluster_version
  role_arn = aws_iam_role.cluster[0].arn

  vpc_config {
    subnet_ids = concat(
      aws_subnet.public[*].id,
      aws_subnet.private[*].id,
    )
  }

  depends_on = [
    aws_iam_role_policy_attachment.cluster_policy,
  ]

  tags = local.common_tags
}

# --- Node group (new cluster only) ---

resource "aws_eks_node_group" "this" {
  count = var.create_cluster ? 1 : 0

  cluster_name    = aws_eks_cluster.this[0].name
  node_group_name = "${local.cluster_name}-nodes"
  node_role_arn   = aws_iam_role.node[0].arn

  subnet_ids = aws_subnet.public[*].id

  instance_types = [var.node_instance_type]

  scaling_config {
    min_size     = var.node_min_size
    max_size     = var.node_max_size
    desired_size = var.node_desired_size
  }

  depends_on = [
    aws_iam_role_policy_attachment.node_worker,
    aws_iam_role_policy_attachment.node_cni,
    aws_iam_role_policy_attachment.node_ecr,
  ]

  tags = local.common_tags
}

# --- Existing cluster (data sources) ---

data "aws_eks_cluster" "existing" {
  count = var.create_cluster ? 0 : 1
  name  = local.cluster_name
}

data "aws_eks_cluster_auth" "existing" {
  count = var.create_cluster ? 0 : 1
  name  = local.cluster_name
}
