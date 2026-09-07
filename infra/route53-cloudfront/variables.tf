variable "aws_region" {
  description = "AWS region for Route53 lookups and non-global resources."
  type        = string
  default     = "us-west-2"
}

variable "zone_name" {
  description = "Public Route53 hosted zone name."
  type        = string
  default     = "robertboscacci.com"
}

variable "laundry_record_name" {
  description = "Laundry monitor subdomain to publish inside the hosted zone."
  type        = string
  default     = "laundry"
}

variable "gotify_record_name" {
  description = "Gotify subdomain to publish inside the hosted zone."
  type        = string
  default     = "gotify"
}

variable "gotify_public_origin_domain" {
  description = "Public Tailscale Funnel DNS name that CloudFront uses as the Gotify origin."
  type        = string
  default     = "optiplex.tailbea63b.ts.net"
}

variable "gotify_public_origin_port" {
  description = "Public Tailscale Funnel HTTPS port that CloudFront uses as the Gotify origin."
  type        = number
  default     = 10000
}

variable "gotify_public_origin_path" {
  description = "Public Tailscale Funnel path that CloudFront prepends for the Gotify origin."
  type        = string
  default     = "/gotify"
}

variable "tailnet_ipv4" {
  description = "Tailscale IPv4 address for the host serving the private apps."
  type        = string
  default     = "100.124.5.39"
}
