output "laundry_url" {
  description = "Custom URL for the laundry monitor."
  value       = "https://${aws_route53_record.laundry_ipv4.fqdn}"
}

output "tailnet_ipv4" {
  description = "Tailnet-only address backing the custom DNS record."
  value       = var.tailnet_ipv4
}
