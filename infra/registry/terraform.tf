terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
  required_version = "~> 1.15.0"
  backend "s3" {
    bucket = "placeholder-tfstate-bucket"
    key    = "placeholder/service/registry.tfstate"
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Project     = "agentic-kie"
      Environment = var.environment
      ManagedBy   = "terraform"
      Purpose     = "registry"
    }
  }
}
