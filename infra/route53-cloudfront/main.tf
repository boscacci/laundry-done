terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
}

locals {
  fqdn = "${var.record_name}.${var.zone_name}"
}

data "aws_route53_zone" "site" {
  name         = var.zone_name
  private_zone = false
}

resource "aws_route53_record" "laundry_ipv4" {
  zone_id = data.aws_route53_zone.site.zone_id
  name    = local.fqdn
  type    = "A"
  ttl     = 300
  records = [var.tailnet_ipv4]
}
