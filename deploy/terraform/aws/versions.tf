terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

# Only used when dr_region_enabled; configured either way so the plan is valid.
provider "aws" {
  alias  = "dr"
  region = var.dr_region
}
