# One-time setup, run by hand with your own AWS credentials (see DEPLOY.md):
# the Terraform state bucket, the container registry, the empty secret, the email
# identity, and the role GitHub Actions assumes to deploy. Everything else is in
# infra/app and is applied by the deploy workflow.

data "aws_caller_identity" "me" {}

locals {
  account   = data.aws_caller_identity.me.account_id
  fn_arns   = "arn:aws:lambda:${var.region}:${local.account}:function:${var.name}-*"
  role_arns = "arn:aws:iam::${local.account}:role/${var.name}-*"
  log_arns  = "arn:aws:logs:${var.region}:${local.account}:log-group:/aws/lambda/${var.name}-*"
}

# --- Terraform state for infra/app -----------------------------------------------
resource "aws_s3_bucket" "state" {
  bucket = "${var.name}-tfstate-${local.account}"
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}

# --- Container registry -------------------------------------------------------------
resource "aws_ecr_repository" "app" {
  name                 = var.name
  image_tag_mutability = "IMMUTABLE" # images are tagged with the git commit
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 5 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}

# Lets the portal's Lambda functions pull the image.
resource "aws_ecr_repository_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "LambdaPull"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
      Condition = { StringLike = { "aws:sourceArn" = local.fn_arns } }
    }]
  })
}

# --- The app's one secret -------------------------------------------------------------
# Terraform creates it empty; you put the JSON value in yourself (DEPLOY.md, step 4),
# so no secret value ever passes through Terraform state or GitHub.
resource "aws_secretsmanager_secret" "app" {
  name                    = "${var.name}/production"
  description             = "Secrets for the HOA portal, as one JSON object."
  recovery_window_in_days = 7
}

# --- Email (Amazon SES) -----------------------------------------------------------------
resource "aws_sesv2_email_identity" "domain" {
  count          = var.email_domain == "" ? 0 : 1
  email_identity = var.email_domain
}

# --- GitHub Actions -> AWS, with no stored keys -----------------------------------------
resource "aws_iam_openid_connect_provider" "github" {
  count          = var.create_github_oidc_provider ? 1 : 0
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 0 : 1
  url   = "https://token.actions.githubusercontent.com"
}

locals {
  oidc_arn = var.create_github_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : data.aws_iam_openid_connect_provider.github[0].arn
}

resource "aws_iam_role" "deploy" {
  name = "github-actions-${var.name}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.oidc_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          # Only the main branch of this one repository can deploy.
          "token.actions.githubusercontent.com:sub" = "repo:${var.github_repo}:ref:refs/heads/main"
        }
      }
    }]
  })
}

# The most the portal's own Lambda role can ever do. The deploy role may only create
# roles that carry this boundary, so a change to infra/app can't grant itself more.
resource "aws_iam_policy" "lambda_boundary" {
  name = "${var.name}-lambda-boundary"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${local.log_arns}:*"
      },
      {
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = aws_secretsmanager_secret.app.arn
      },
      {
        Effect   = "Allow"
        Action   = ["ses:SendEmail", "ses:SendRawEmail"]
        Resource = "arn:aws:ses:${var.region}:${local.account}:*"
      },
      {
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = local.fn_arns
      },
    ]
  })
}

resource "aws_iam_role_policy" "deploy" {
  name = "deploy"
  role = aws_iam_role.deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "State"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.state.arn
      },
      {
        Sid      = "StateObjects"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.state.arn}/*"
      },
      {
        Sid      = "EcrLogin"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid    = "EcrPush"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer", "ecr:DescribeRepositories", "ecr:DescribeImages",
          "ecr:GetRepositoryPolicy", "ecr:ListTagsForResource",
        ]
        Resource = aws_ecr_repository.app.arn
      },
      {
        Sid    = "Functions"
        Effect = "Allow"
        Action = [
          "lambda:CreateFunction", "lambda:DeleteFunction", "lambda:UpdateFunctionCode",
          "lambda:UpdateFunctionConfiguration", "lambda:AddPermission",
          "lambda:RemovePermission", "lambda:InvokeFunction", "lambda:TagResource",
          "lambda:UntagResource", "lambda:Get*", "lambda:List*",
          "lambda:PutFunctionConcurrency", "lambda:DeleteFunctionConcurrency",
        ]
        Resource = local.fn_arns
      },
      {
        Sid      = "AppRoles"
        Effect   = "Allow"
        Action   = ["iam:CreateRole", "iam:PutRolePolicy", "iam:AttachRolePolicy"]
        Resource = local.role_arns
        Condition = {
          StringEquals = { "iam:PermissionsBoundary" = aws_iam_policy.lambda_boundary.arn }
        }
      },
      {
        Sid    = "AppRolesManage"
        Effect = "Allow"
        Action = [
          "iam:GetRole", "iam:DeleteRole", "iam:UpdateRole", "iam:TagRole", "iam:UntagRole",
          "iam:GetRolePolicy", "iam:DeleteRolePolicy", "iam:DetachRolePolicy",
          "iam:ListRolePolicies", "iam:ListAttachedRolePolicies",
          "iam:ListInstanceProfilesForRole", "iam:UpdateAssumeRolePolicy",
        ]
        Resource = local.role_arns
      },
      {
        Sid       = "PassAppRoles"
        Effect    = "Allow"
        Action    = "iam:PassRole"
        Resource  = local.role_arns
        Condition = { StringEquals = { "iam:PassedToService" = "lambda.amazonaws.com" } }
      },
      {
        Sid      = "KeepTheBoundary"
        Effect   = "Deny"
        Action   = ["iam:DeleteRolePermissionsBoundary", "iam:PutRolePermissionsBoundary"]
        Resource = "*"
      },
      {
        Sid      = "ReadBoundary"
        Effect   = "Allow"
        Action   = ["iam:GetPolicy", "iam:GetPolicyVersion"]
        Resource = aws_iam_policy.lambda_boundary.arn
      },
      {
        Sid      = "FindBoundary"
        Effect   = "Allow"
        Action   = "iam:ListPolicies"
        Resource = "*"
      },
      {
        Sid    = "Logs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:PutRetentionPolicy",
          "logs:DeleteRetentionPolicy", "logs:TagResource", "logs:UntagResource",
          "logs:ListTagsForResource", "logs:ListTagsLogGroup", "logs:TagLogGroup",
        ]
        Resource = [local.log_arns, "${local.log_arns}:*"]
      },
      {
        Sid      = "LogsDescribe"
        Effect   = "Allow"
        Action   = "logs:DescribeLogGroups"
        Resource = "*"
      },
      {
        Sid    = "Api"
        Effect = "Allow"
        Action = ["apigateway:GET", "apigateway:POST", "apigateway:PUT", "apigateway:PATCH", "apigateway:DELETE", "apigateway:TagResource", "apigateway:UntagResource"]
        Resource = [
          "arn:aws:apigateway:${var.region}::/apis",
          "arn:aws:apigateway:${var.region}::/apis/*",
          "arn:aws:apigateway:${var.region}::/domainnames",
          "arn:aws:apigateway:${var.region}::/domainnames/*",
          "arn:aws:apigateway:${var.region}::/tags/*",
        ]
      },
      {
        Sid    = "Schedule"
        Effect = "Allow"
        Action = [
          "events:PutRule", "events:DeleteRule", "events:DescribeRule", "events:PutTargets",
          "events:RemoveTargets", "events:ListTargetsByRule", "events:ListTagsForResource",
          "events:TagResource", "events:UntagResource",
        ]
        Resource = "arn:aws:events:${var.region}:${local.account}:rule/${var.name}-*"
      },
      {
        Sid      = "ReadSecretMetadata"
        Effect   = "Allow"
        Action   = ["secretsmanager:DescribeSecret", "secretsmanager:GetResourcePolicy"]
        Resource = aws_secretsmanager_secret.app.arn
      },
      {
        Sid      = "CustomDomainCertificate"
        Effect   = "Allow"
        Action   = ["acm:DescribeCertificate", "acm:ListCertificates"]
        Resource = "*"
      },
    ]
  })
}
