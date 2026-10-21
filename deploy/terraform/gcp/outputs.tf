output "connection_name" {
  value = google_sql_database_instance.this.connection_name
}

output "private_ip" {
  value = google_sql_database_instance.this.private_ip_address
}

output "hub_database_url_hint" {
  description = "The shape of HUB_DATABASE_URL; use the least-privileged hub role, not the owner."
  value       = "postgresql+asyncpg://<hub_app_role>:<password>@${google_sql_database_instance.this.private_ip_address}:5432/unimem_hub?ssl=require"
}
