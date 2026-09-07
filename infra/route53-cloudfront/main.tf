terraform {
  required_version = ">= 1.6.0"

  backend "s3" {
    bucket         = "laundry-done-opentofu-state-062008221187"
    key            = "route53-cloudfront/tofu.tfstate"
    region         = "us-west-2"
    dynamodb_table = "iac-dev-box-tf-locks-062008221187-us-west-2"
    encrypt        = true
  }

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
  laundry_fqdn     = "${var.laundry_record_name}.${var.zone_name}"
  gotify_fqdn      = "${var.gotify_record_name}.${var.zone_name}"
  gotify_origin_id = "gotify-tailscale-funnel"

  tags = {
    ManagedBy = "opentofu"
    Project   = "laundry-done"
  }
}

data "aws_route53_zone" "site" {
  name         = var.zone_name
  private_zone = false
}

data "aws_cloudfront_cache_policy" "caching_disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "all_viewer_except_host_header" {
  name = "Managed-AllViewerExceptHostHeader"
}

resource "aws_route53_record" "laundry_ipv4" {
  zone_id = data.aws_route53_zone.site.zone_id
  name    = local.laundry_fqdn
  type    = "A"
  ttl     = 300
  records = [var.tailnet_ipv4]
}

resource "aws_acm_certificate" "gotify_public" {
  provider = aws.us_east_1

  domain_name       = local.gotify_fqdn
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }

  tags = merge(local.tags, {
    Service = "gotify"
  })
}

resource "aws_route53_record" "gotify_cert_validation" {
  for_each = toset([local.gotify_fqdn])

  allow_overwrite = true
  name            = one([for option in aws_acm_certificate.gotify_public.domain_validation_options : option.resource_record_name])
  records         = [one([for option in aws_acm_certificate.gotify_public.domain_validation_options : option.resource_record_value])]
  ttl             = 60
  type            = one([for option in aws_acm_certificate.gotify_public.domain_validation_options : option.resource_record_type])
  zone_id         = data.aws_route53_zone.site.zone_id
}

resource "aws_acm_certificate_validation" "gotify_public" {
  provider = aws.us_east_1

  certificate_arn         = aws_acm_certificate.gotify_public.arn
  validation_record_fqdns = [for record in aws_route53_record.gotify_cert_validation : record.fqdn]
}

resource "aws_cloudfront_distribution" "gotify_public" {
  enabled         = true
  is_ipv6_enabled = true
  comment         = "Public Gotify front door for laundry-done via Tailscale Funnel"
  aliases         = [local.gotify_fqdn]
  price_class     = "PriceClass_100"

  origin {
    domain_name = var.gotify_public_origin_domain
    origin_id   = local.gotify_origin_id
    origin_path = var.gotify_public_origin_path

    custom_origin_config {
      http_port                = 80
      https_port               = var.gotify_public_origin_port
      origin_keepalive_timeout = 5
      origin_protocol_policy   = "https-only"
      origin_read_timeout      = 30
      origin_ssl_protocols     = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    allowed_methods          = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.caching_disabled.id
    compress                 = true
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer_except_host_header.id
    target_origin_id         = local.gotify_origin_id
    viewer_protocol_policy   = "redirect-to-https"
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.gotify_public.certificate_arn
    minimum_protocol_version = "TLSv1.2_2021"
    ssl_support_method       = "sni-only"
  }

  tags = merge(local.tags, {
    Service = "gotify"
  })
}

resource "aws_route53_record" "gotify_ipv4" {
  zone_id = data.aws_route53_zone.site.zone_id
  name    = local.gotify_fqdn
  type    = "A"

  alias {
    evaluate_target_health = false
    name                   = aws_cloudfront_distribution.gotify_public.domain_name
    zone_id                = aws_cloudfront_distribution.gotify_public.hosted_zone_id
  }
}

resource "aws_route53_record" "gotify_ipv6" {
  zone_id = data.aws_route53_zone.site.zone_id
  name    = local.gotify_fqdn
  type    = "AAAA"

  alias {
    evaluate_target_health = false
    name                   = aws_cloudfront_distribution.gotify_public.domain_name
    zone_id                = aws_cloudfront_distribution.gotify_public.hosted_zone_id
  }
}
