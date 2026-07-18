variable "project_id" {
  description = "GCP project ID. Must already exist with billing linked (see docs/design-decisions.md)."
  type        = string
}

variable "region" {
  description = "GCP region for the raw bucket and BigQuery dataset locations."
  type        = string
  default     = "us-central1"
}

variable "matching_dataset_expiration_days" {
  description = <<-EOT
    Default table expiration for the `matching` dataset, in days. candidate_pairs and
    pair_scores are disposable intermediates (PROJECT_CONSTITUTION.md #9) -- 7 days matches
    the doc's storage layout table.
  EOT
  type        = number
  default     = 7
}
