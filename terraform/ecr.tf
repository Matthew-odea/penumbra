# ── ECR repository ─────────────────────────────────────────────────────────

resource "aws_ecr_repository" "penumbra" {
  name                 = "penumbra"
  image_tag_mutability = "MUTABLE" # :latest tag is overwritten on each deploy

  image_scanning_configuration {
    scan_on_push = true
  }
}

# Keep only the last 10 images to control storage costs (~$0.10/GB/month)
resource "aws_ecr_lifecycle_policy" "penumbra" {
  repository = aws_ecr_repository.penumbra.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}
