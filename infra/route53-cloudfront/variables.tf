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

variable "record_name" {
  description = "Subdomain to publish inside the hosted zone."
  type        = string
  default     = "laundry"
}

variable "tailnet_ipv4" {
  description = "Tailscale IPv4 address for the host serving the monitor."
  type        = string
  default     = "100.124.5.39"
}
