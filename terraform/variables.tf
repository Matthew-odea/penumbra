variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "us-east-1"
}

variable "github_repo" {
  description = "GitHub repository in owner/name format (e.g. 'acme/penumbra'). Used in OIDC trust policy."
  type        = string
}

# ── App secrets (written to SSM Parameter Store) ───────────────────────────
# Put these in secrets.tfvars (gitignored). Run: terraform apply -var-file=secrets.tfvars

variable "polymarket_private_key" {
  description = "Polygon wallet private key for Polymarket L2 auth"
  type        = string
  sensitive   = true
  default     = ""
}

variable "polymarket_api_key" {
  description = "Polymarket CLOB API key"
  type        = string
  sensitive   = true
  default     = ""
}

variable "polymarket_api_secret" {
  description = "Polymarket CLOB API secret"
  type        = string
  sensitive   = true
  default     = ""
}

variable "polymarket_api_passphrase" {
  description = "Polymarket CLOB API passphrase"
  type        = string
  sensitive   = true
  default     = ""
}

variable "tavily_api_key" {
  description = "Tavily search API key"
  type        = string
  sensitive   = true
  default     = ""
}

variable "alchemy_api_key" {
  description = "Alchemy Polygon RPC API key"
  type        = string
  sensitive   = true
  default     = ""
}
