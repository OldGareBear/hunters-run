output "url" {
  description = "Where the portal is."
  value       = local.base_url
}

output "api_endpoint" {
  value = aws_apigatewayv2_api.http.api_endpoint
}

output "tasks_function" {
  value = aws_lambda_function.tasks.function_name
}

output "custom_domain_target" {
  description = "Point a CNAME for custom_domain here."
  value       = var.custom_domain == "" ? "" : aws_apigatewayv2_domain_name.custom[0].domain_name_configuration[0].target_domain_name
}
