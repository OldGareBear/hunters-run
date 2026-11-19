terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
  # Bucket and region come from the deploy workflow (-backend-config), so nothing
  # account-specific is committed.
  backend "s3" {
    key          = "app.tfstate"
    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  default_tags {
    tags = { app = var.name }
  }
}
