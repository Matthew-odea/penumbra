output "dashboard_url" {
  description = "Public dashboard URL"
  value       = "http://${aws_eip.penumbra.public_ip}:8000"
}

output "elastic_ip" {
  description = "Elastic IP address — point your DNS A record here when ready for HTTPS"
  value       = aws_eip.penumbra.public_ip
}

output "ecr_repository_url" {
  description = "ECR repository URL for Docker push"
  value       = aws_ecr_repository.penumbra.repository_url
}

output "ec2_instance_id" {
  description = "EC2 instance ID — set as INSTANCE_ID variable in your GitHub repo"
  value       = aws_instance.penumbra.id
}

output "github_actions_role_arn" {
  description = "IAM role ARN for GitHub Actions OIDC — set as AWS_ROLE_ARN variable in your GitHub repo"
  value       = aws_iam_role.github_actions.arn
}

output "ssm_connect_command" {
  description = "Command to open a browser session to the EC2 instance via SSM (no SSH needed)"
  value       = "aws ssm start-session --target ${aws_instance.penumbra.id} --region ${var.aws_region}"
}
