variable "name" {
  description = "Same prefix as infra/bootstrap."
  type        = string
  default     = "hoa-portal"
}

variable "secret_version" {
  description = "The secret's current version id (set by the deploy workflow). A new value restarts the functions so they read the new secret."
  type        = string
  default     = ""
}

variable "image_tag" {
  description = "Container image tag to run (the deploy workflow passes the git commit)."
  type        = string
}

variable "hoa_name" {
  description = "Shown in the header and in emails."
  type        = string
}

variable "email_from" {
  description = "Sender, e.g. \"Your HOA <yourhoa@gmail.com>\". With Gmail it must be the account's own address; with SES, an address on email_domain."
  type        = string
}

variable "email_provider" {
  description = "smtp (the HOA's Gmail account, the default; no domain needed) or ses (needs email_domain verified by infra/bootstrap). Empty means smtp."
  type        = string
  default     = "smtp"
  validation {
    condition     = contains(["", "smtp", "ses"], var.email_provider)
    error_message = "email_provider must be smtp or ses."
  }
}

variable "email_domain" {
  description = "Only for email_provider = ses: the domain verified in SES by infra/bootstrap."
  type        = string
  default     = ""
}

variable "smtp_host" {
  description = "For email_provider = smtp. The login and app password are in the secret (DEPLOY.md, step 4)."
  type        = string
  default     = "smtp.gmail.com"
}

variable "smtp_port" {
  description = "For email_provider = smtp: 587 with STARTTLS."
  type        = number
  default     = 587
}

variable "inbox_address" {
  description = "Optional: the Gmail address people cc so the weekly review reads the chain (DEPLOY.md, step 10). Usually the account email is sent from."
  type        = string
  default     = ""
}

variable "management_notify_emails" {
  description = "Extra addresses that get homeowners' questions to management, comma separated."
  type        = string
  default     = ""
}

variable "hoa_timezone" {
  type    = string
  default = "America/New_York"
}

variable "custom_domain" {
  description = "Optional, e.g. portal.your-domain.org. Needs custom_domain_certificate_arn."
  type        = string
  default     = ""
}

variable "custom_domain_certificate_arn" {
  description = "An ISSUED ACM certificate for custom_domain, in this region."
  type        = string
  default     = ""
}

variable "worker_schedule" {
  description = "How often to sync Dropbox, retry email and tidy up. Hourly lets the database sleep."
  type        = string
  default     = "rate(1 hour)"
}

variable "anthropic_model" {
  description = "The portal knows claude-haiku-5-5, claude-sonnet-5-5 and claude-opus-5-5. For any other model, also set both chat_price variables."
  type        = string
  default     = "claude-haiku-5-5"
}

variable "chat_price_input_per_mtok" {
  description = "USD per million input tokens, for the budget. Leave empty for a model the portal knows."
  type        = string
  default     = ""
}

variable "chat_price_output_per_mtok" {
  description = "USD per million output tokens, for the budget. Leave empty for a model the portal knows."
  type        = string
  default     = ""
}

variable "chat_daily_limit_per_user" {
  description = "Questions per person per day. 0 = no limit."
  type        = number
  default     = 20
}

variable "chat_monthly_budget_usd" {
  description = "Estimated assistant spend per month before it pauses. 0 = no limit."
  type        = number
  default     = 5
}

variable "log_retention_days" {
  type    = number
  default = 30
}
