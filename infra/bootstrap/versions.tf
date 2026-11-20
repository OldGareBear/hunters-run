terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
  # Local state on purpose: this is run once, by hand, before the state bucket exists.
  # Keep the terraform.tfstate file it writes (it holds no secret values).
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { app = var.name }
  }
}
