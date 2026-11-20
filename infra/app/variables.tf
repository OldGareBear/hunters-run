variable "name" {
  description = "Same prefix as infra/bootstrap."
  type        = string
  default     = "hoa-portal"
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
  description = "Sender, e.g. \"Your HOA <no-reply@your-domain.org>\". Must be on email_domain."
  type        = string
}

variable "email_domain" {
  description = "The domain verified in SES by infra/bootstrap."
  type        = string
}

variable "management_notify_emails" {
  description = "Extra addresses told about new tickets, comma separated."
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
