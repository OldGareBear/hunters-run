output "github_variables" {
  description = "Set these as GitHub repository variables (DEPLOY.md, step 5)."
  value = {
    AWS_REGION          = var.region
    AWS_DEPLOY_ROLE_ARN = aws_iam_role.deploy.arn
    TF_STATE_BUCKET     = aws_s3_bucket.state.bucket
  }
}

output "secret_name" {
  value = aws_secretsmanager_secret.app.name
}

output "ses_dkim_records" {
  description = "Add these three CNAME records at your DNS host so SES can send for the domain."
  value = var.email_domain == "" ? [] : [
    for t in aws_sesv2_email_identity.domain[0].dkim_signing_attributes[0].tokens :
    { type = "CNAME", name = "${t}._domainkey.${var.email_domain}", value = "${t}.dkim.amazonses.com" }
  ]
}
