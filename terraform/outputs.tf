output "project_id" {
  value = var.project_id
}

output "raw_bucket_name" {
  value = google_storage_bucket.raw.name
}

output "pipeline_service_account_email" {
  value = google_service_account.pipeline.email
}

output "bigquery_datasets" {
  value = concat(
    [for d in google_bigquery_dataset.permanent : d.dataset_id],
    [google_bigquery_dataset.matching.dataset_id],
  )
}
