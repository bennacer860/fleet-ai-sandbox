variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "eu-west-1"
}

variable "instance_type" {
  description = "EC2 instance type (must be ARM64 compatible like t4g)"
  type        = string
  default     = "t4g.nano"
}

variable "use_spot_instance" {
  description = "Whether to use a Spot Instance to save costs"
  type        = bool
  default     = true
}

variable "ssm_parameter_prefix" {
  description = "Prefix for SSM parameters containing bot secrets"
  type        = string
  default     = "/polymarket-bot/"
}
