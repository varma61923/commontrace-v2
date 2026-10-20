variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "name" {
  type    = string
  default = "commontrace-hub"
}

variable "network_self_link" {
  type        = string
  description = "VPC with private services access already set up; the database gets no public address."
}

variable "tier" {
  type    = string
  default = "db-custom-4-16384"
}

variable "disk_size_gb" {
  type    = number
  default = 200
}

variable "high_availability" {
  type    = bool
  default = true
}

variable "kms_key_name" {
  type        = string
  description = "Customer-managed key (CMEK) for the instance and its backups, in the same region."
}

variable "backup_retention_days" {
  type        = number
  default     = 14
  description = "Number of daily backups retained, independent of the transaction-log recovery window."
  validation {
    condition     = var.backup_retention_days >= 7 && var.backup_retention_days <= 35
    error_message = "Retain between 7 and 35 daily backups."
  }
}

variable "transaction_log_retention_days" {
  type        = number
  default     = 7
  description = "PITR transaction-log retention for the Enterprise edition (1 to 7 days)."
  validation {
    condition = (
      var.transaction_log_retention_days >= 1 &&
      var.transaction_log_retention_days <= 7 &&
      floor(var.transaction_log_retention_days) == var.transaction_log_retention_days
    )
    error_message = "Enterprise edition transaction-log retention must be an integer from 1 to 7 days."
  }
}

variable "deletion_protection" {
  type    = bool
  default = true
}

variable "max_connections" {
  type        = number
  default     = 400
  description = "Must exceed (pool + overflow) x (max replicas + surge) of the Hub."
}
