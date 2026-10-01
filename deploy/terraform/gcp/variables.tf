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
  description = "Point-in-time recovery keeps transaction logs for this many days (1 to 35)."
  validation {
    condition     = var.backup_retention_days >= 7 && var.backup_retention_days <= 35
    error_message = "Use 7 to 35 days of log retention."
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
