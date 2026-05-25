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

variable "tailnet_ipv4" {
  description = "Tailscale IPv4 address for the host serving the private apps."
  type        = string
  default     = "100.124.5.39"
}
