mock_provider "google" {}

variables {
  project_id        = "test-project"
  region            = "us-central1"
  network_self_link = "projects/test-project/global/networks/private"
  kms_key_name      = "projects/test-project/locations/us-central1/keyRings/test/cryptoKeys/database"
}

run "default_retention_windows_are_independent" {
  command = plan

  assert {
    condition = (
      google_sql_database_instance.this.settings[0].backup_configuration[0].transaction_log_retention_days == 7 &&
      google_sql_database_instance.this.settings[0].backup_configuration[0].backup_retention_settings[0].retained_backups == 14
    )
    error_message = "Daily backups and Enterprise PITR logs need separate supported retention windows."
  }
}

run "unsupported_enterprise_window_is_rejected" {
  command = plan
  variables {
    transaction_log_retention_days = 8
  }
  expect_failures = [var.transaction_log_retention_days]
}

run "fractional_log_retention_is_rejected" {
  command = plan
  variables {
    transaction_log_retention_days = 1.5
  }
  expect_failures = [var.transaction_log_retention_days]
}
