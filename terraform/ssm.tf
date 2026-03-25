# ── SSM Parameter Store — app secrets ──────────────────────────────────────
# Values are written here by `terraform apply -var-file=secrets.tfvars`.
# The EC2 instance reads them at startup via the IAM role.
# Leave the default "" if you haven't obtained a key yet — update later
# with: terraform apply -var-file=secrets.tfvars

locals {
  params = {
    polymarket_private_key    = var.polymarket_private_key
    polymarket_api_key        = var.polymarket_api_key
    polymarket_api_secret     = var.polymarket_api_secret
    polymarket_api_passphrase = var.polymarket_api_passphrase
    tavily_api_key            = var.tavily_api_key
    alchemy_api_key           = var.alchemy_api_key
  }
}

resource "aws_ssm_parameter" "app_secrets" {
  for_each = local.params

  name  = "/penumbra/${each.key}"
  type  = "SecureString" # KMS-encrypted, free for standard parameters
  value = each.value != "" ? each.value : "PLACEHOLDER"

  lifecycle {
    # Don't overwrite with PLACEHOLDER if someone has set a real value
    # directly in the console or via CLI after initial apply.
    ignore_changes = [value]
  }
}
