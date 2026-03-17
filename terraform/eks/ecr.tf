# =============================================================================
# ECR Repositories
# =============================================================================

resource "aws_ecr_repository" "this" {
  for_each = toset(local.ecr_components)

  name         = each.value
  force_delete = false

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = local.common_tags
}
