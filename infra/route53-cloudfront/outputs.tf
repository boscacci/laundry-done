output "laundry_url" {
  description = "Tailnet-only custom URL for the laundry monitor."
  value       = "https://${aws_route53_record.laundry_ipv4.fqdn}"
}

output "gotify_url" {
  description = "Public custom URL for Gotify."
  value       = "https://${local.gotify_fqdn}"
}

output "gotify_cloudfront_domain_name" {
  description = "CloudFront distribution DNS name backing the public Gotify URL."
  value       = aws_cloudfront_distribution.gotify_public.domain_name
}

output "gotify_public_origin_url" {
  description = "Public Tailscale Funnel origin URL that CloudFront forwards to."
  value       = "https://${var.gotify_public_origin_domain}:${var.gotify_public_origin_port}${var.gotify_public_origin_path}"
}

output "tailnet_ipv4" {
  description = "Tailnet-only address backing the private laundry DNS record."
  value       = var.tailnet_ipv4
}
