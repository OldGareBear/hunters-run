variable "region" {
  description = "AWS region for everything, e.g. us-east-1."
  type        = string
  default     = "us-east-1"
}

variable "name" {
  description = "Prefix for every resource. Changing it later means recreating everything."
  type        = string
  default     = "hoa-portal"
}

variable "github_repo" {
  description = "The GitHub repository allowed to deploy, as owner/name."
  type        = string
}

variable "email_domain" {
  description = "Domain the portal sends email from (its DNS must be yours). Leave empty to set up email later."
  type        = string
  default     = ""
}

variable "create_github_oidc_provider" {
  description = "Set false if this AWS account already has the GitHub Actions OIDC provider."
  type        = bool
  default     = true
}
