output "endpoint" {
  value = aws_db_instance.this.address
}

output "master_secret_arn" {
  description = "Secrets Manager secret holding the database owner credentials."
  value       = aws_db_instance.this.master_user_secret[0].secret_arn
}

output "kms_key_arn" {
  value = local.key_arn
}

output "latest_restorable_time" {
  description = "How recent a point-in-time restore can be right now."
  value       = aws_db_instance.this.latest_restorable_time
}

output "hub_database_url_hint" {
  description = "The shape of HUB_DATABASE_URL; use the least-privileged hub role (hub/DEPLOYMENT.md), not the owner."
  value       = "postgresql+asyncpg://<hub_app_role>:<password>@${aws_db_instance.this.address}:5432/commontrace_hub?ssl=require"
}
