# The portal on AWS Lambda behind an API Gateway HTTP API. No VPC: the functions
# reach Neon, Dropbox and the Anthropic API over the internet, so there's no NAT
# gateway to pay for. Applied by .github/workflows/deploy.yml.

data "aws_caller_identity" "me" {}
data "aws_region" "current" {}

data "aws_ecr_repository" "app" {
  name = var.name
}

data "aws_secretsmanager_secret" "app" {
  name = "${var.name}/production"
}

data "aws_iam_policy" "boundary" {
  name = "${var.name}-lambda-boundary"
}

locals {
  region         = data.aws_region.current.region
  account        = data.aws_caller_identity.me.account_id
  image          = "${data.aws_ecr_repository.app.repository_url}:${var.image_tag}"
  tasks_function = "${var.name}-tasks"
  base_url       = var.custom_domain != "" ? "https://${var.custom_domain}" : aws_apigatewayv2_api.http.api_endpoint

  # Non-secret settings. The secrets (database URL, API keys, Dropbox) come from the
  # Secrets Manager secret at start-up; see DEPLOY.md for its contents.
  env = {
    APP_ENV                       = "production"
    AWS_SECRETS_MANAGER_SECRET_ID = data.aws_secretsmanager_secret.app.name
    SECRET_VERSION                = var.secret_version # not read by the app; see variables.tf
    HOA_NAME                      = var.hoa_name
    BASE_URL                      = local.base_url
    SESSION_COOKIE_SECURE         = "true"
    HOA_TIMEZONE                  = var.hoa_timezone
    DATABASE_NULL_POOL            = "true"
    TASKS_FUNCTION_NAME           = local.tasks_function
    DOCUMENT_SOURCE               = "dropbox"
    DOWNLOAD_REDIRECT_OVER_MB     = "4"
    RETRIEVAL                     = "keyword"
    EMBEDDING_PROVIDER            = "hashing"
    LLM_PROVIDER                  = "anthropic"
    ANTHROPIC_MODEL               = var.anthropic_model
    CHAT_PRICE_INPUT_PER_MTOK     = var.chat_price_input_per_mtok
    CHAT_PRICE_OUTPUT_PER_MTOK    = var.chat_price_output_per_mtok
    CHAT_DAILY_LIMIT_PER_USER     = tostring(var.chat_daily_limit_per_user)
    CHAT_MONTHLY_BUDGET_USD       = tostring(var.chat_monthly_budget_usd)
    EMAIL_PROVIDER                = "ses"
    SES_REGION                    = local.region
    EMAIL_FROM                    = var.email_from
    EMAIL_SEND_INLINE             = "true"
    MANAGEMENT_NOTIFY_EMAILS      = var.management_notify_emails
    PYTHONDONTWRITEBYTECODE       = "1"
  }
}

# --- Permissions for the functions --------------------------------------------------
resource "aws_iam_role" "lambda" {
  name                 = "${var.name}-lambda"
  permissions_boundary = data.aws_iam_policy.boundary.arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda" {
  name = "app"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["${aws_cloudwatch_log_group.web.arn}:*", "${aws_cloudwatch_log_group.tasks.arn}:*"]
      },
      {
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = data.aws_secretsmanager_secret.app.arn
      },
      {
        # Every identity in this account and region, not just the sending domain: in the
        # SES sandbox, SES also checks the (verified) recipient's identity against this.
        Effect   = "Allow"
        Action   = ["ses:SendEmail", "ses:SendRawEmail"]
        Resource = "arn:aws:ses:${local.region}:${local.account}:identity/*"
      },
      {
        # The web function hands email and Dropbox syncs to the tasks function.
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = aws_lambda_function.tasks.arn
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "web" {
  name              = "/aws/lambda/${var.name}-web"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "tasks" {
  name              = "/aws/lambda/${local.tasks_function}"
  retention_in_days = var.log_retention_days
}

# --- Functions (one image, two jobs) ------------------------------------------------
resource "aws_lambda_function" "web" {
  function_name = "${var.name}-web"
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = local.image
  architectures = ["x86_64"]
  memory_size   = 1024
  timeout       = 29 # API Gateway gives up at 30 seconds
  environment {
    variables = local.env
  }
  depends_on = [aws_cloudwatch_log_group.web]
}

resource "aws_lambda_function" "tasks" {
  function_name = local.tasks_function
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = local.image
  architectures = ["x86_64"]
  memory_size   = 1024
  timeout       = 900 # a first Dropbox import can take a few minutes
  environment {
    variables = local.env
  }
  depends_on = [aws_cloudwatch_log_group.tasks]
}

# --- HTTPS front door ----------------------------------------------------------------
resource "aws_apigatewayv2_api" "http" {
  name          = var.name
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "web" {
  api_id                 = aws_apigatewayv2_api.http.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.web.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 29000
}

resource "aws_apigatewayv2_route" "all" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.web.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.http.id
  name        = "$default"
  auto_deploy = true
  # Plenty for a few dozen people, and a ceiling on what abuse could cost.
  default_route_settings {
    throttling_rate_limit  = 20
    throttling_burst_limit = 40
  }
}

resource "aws_lambda_permission" "api" {
  statement_id  = "AllowApiGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.web.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*/*"
}

resource "aws_apigatewayv2_domain_name" "custom" {
  count       = var.custom_domain == "" ? 0 : 1
  domain_name = var.custom_domain
  domain_name_configuration {
    certificate_arn = var.custom_domain_certificate_arn
    endpoint_type   = "REGIONAL"
    security_policy = "TLS_1_2"
  }
}

resource "aws_apigatewayv2_api_mapping" "custom" {
  count       = var.custom_domain == "" ? 0 : 1
  api_id      = aws_apigatewayv2_api.http.id
  domain_name = aws_apigatewayv2_domain_name.custom[0].id
  stage       = aws_apigatewayv2_stage.default.id
}

# --- Schedule: Dropbox sync, email retries, tidy-up ---------------------------------
resource "aws_cloudwatch_event_rule" "worker" {
  name                = "${var.name}-worker"
  schedule_expression = var.worker_schedule
}

resource "aws_cloudwatch_event_target" "worker" {
  rule  = aws_cloudwatch_event_rule.worker.name
  arn   = aws_lambda_function.tasks.arn
  input = jsonencode({ task = "worker" })
}

resource "aws_lambda_permission" "schedule" {
  statement_id  = "AllowSchedule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.tasks.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.worker.arn
}
