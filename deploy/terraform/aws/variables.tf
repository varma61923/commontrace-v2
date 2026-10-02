variable "region" {
  type = string
}

variable "dr_region" {
  type        = string
  default     = "us-west-2"
  description = "Where replicated backups go when dr_region_enabled."
}

variable "name" {
  type        = string
  description = "Prefix for every resource."
  default     = "commontrace-hub"
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  type        = list(string)
  description = "Private subnets in at least two availability zones."
  validation {
    condition     = length(var.subnet_ids) >= 2
    error_message = "Give at least two subnets in different availability zones."
  }
}

variable "allowed_security_group_ids" {
  type        = list(string)
  description = "Security groups (the cluster's nodes) allowed to reach Postgres."
}

variable "instance_class" {
  type    = string
  default = "db.r6g.large"
}

variable "allocated_storage_gb" {
  type    = number
  default = 200
}

variable "max_allocated_storage_gb" {
  type        = number
  default     = 2000
  description = "Storage autoscaling ceiling."
}

variable "multi_az" {
  type    = bool
  default = true
}

variable "backup_retention_days" {
  type        = number
  default     = 14
  description = "Point-in-time recovery window. Zero would disable backups and PITR, so it is refused."
  validation {
    condition     = var.backup_retention_days >= 7 && var.backup_retention_days <= 35
    error_message = "Use 7 to 35 days: below 7 is not a recovery window, above 35 is not offered by RDS."
  }
}

variable "kms_key_arn" {
  type        = string
  default     = ""
  description = "Bring your own customer-managed key. Empty creates one for this database."
}

variable "dr_region_enabled" {
  type        = bool
  default     = false
  description = "Replicate automated backups to the aws.dr provider's region, for a regional outage."
}

variable "dr_kms_key_arn" {
  type        = string
  default     = ""
  description = "Customer-managed key in the DR region, required when dr_region_enabled."
}

variable "deletion_protection" {
  type    = bool
  default = true
}

variable "alarm_topic_arn" {
  type        = string
  default     = ""
  description = "SNS topic for the database alarms. Empty creates the alarms without a destination."
}

variable "max_connections" {
  type        = number
  default     = 400
  description = "Set Postgres max_connections; must exceed (pool + overflow) x (max replicas + surge) of the Hub."
}
