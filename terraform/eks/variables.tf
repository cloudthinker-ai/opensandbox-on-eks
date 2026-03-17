variable "create_cluster" {
  description = "true = create new EKS cluster, false = use existing"
  type        = bool
  default     = true
}

variable "cluster_name" {
  description = "EKS cluster name"
  type        = string
  default     = "opensandbox"
}

variable "aws_profile" {
  description = "AWS CLI profile name"
  type        = string
  default     = null
}

variable "region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "cluster_version" {
  description = "EKS Kubernetes version"
  type        = string
  default     = "1.29"
}

variable "node_instance_type" {
  description = "EC2 instance type for EKS node group"
  type        = string
  default     = "t3.small"
}

variable "node_min_size" {
  description = "Minimum number of nodes in the node group"
  type        = number
  default     = 1
}

variable "node_max_size" {
  description = "Maximum number of nodes in the node group"
  type        = number
  default     = 2
}

variable "node_desired_size" {
  description = "Desired number of nodes in the node group"
  type        = number
  default     = 1
}

variable "vpc_cidr" {
  description = "VPC CIDR block (new cluster only)"
  type        = string
  default     = "10.0.0.0/16"
}

variable "install_ebs_csi" {
  description = "Install EBS CSI driver addon"
  type        = bool
  default     = true
}

variable "install_snapshot_controller" {
  description = "Install snapshot controller addon"
  type        = bool
  default     = true
}

variable "enable_network_policy" {
  description = "Enable VPC CNI NetworkPolicy support"
  type        = bool
  default     = true
}
