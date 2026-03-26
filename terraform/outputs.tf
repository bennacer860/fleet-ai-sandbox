output "instance_id" {
  description = "ID of the EC2 instance"
  value       = aws_instance.bot_instance.id
}

output "s3_bucket_name" {
  description = "Name of the S3 bucket used for Litestream backups"
  value       = aws_s3_bucket.bot_backups.id
}

output "ssm_connect_command" {
  description = "Command to connect to the instance via SSM Session Manager"
  value       = "aws ssm start-session --target ${aws_instance.bot_instance.id} --region ${var.aws_region}"
}
