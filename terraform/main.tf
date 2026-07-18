terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# Vendor drop zone (PROJECT_CONSTITUTION.md #9). Empty until scripts/upload_to_gcs.py or a
# scale-tier generation run populates it -- an empty bucket costs nothing.
resource "google_storage_bucket" "raw" {
  name                        = "${var.project_id}-mdm-raw"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true # portfolio project: `terraform destroy` must leave nothing behind (P10)
}

# Five of the six datasets in PROJECT_CONSTITUTION.md #9's storage layout table are permanent
# (no default expiration) -- raw_standard, conformance, serving, quality, snapshots. `matching`
# is the one exception: candidate_pairs/pair_scores are disposable intermediates, not
# something worth keeping around accumulating storage cost across runs.
locals {
  permanent_datasets = ["raw_standard", "conformance", "serving", "quality", "snapshots"]
}

resource "google_bigquery_dataset" "permanent" {
  for_each   = toset(local.permanent_datasets)
  dataset_id = each.value
  location   = var.region
}

resource "google_bigquery_dataset" "matching" {
  dataset_id                  = "matching"
  location                    = var.region
  default_table_expiration_ms = var.matching_dataset_expiration_days * 24 * 60 * 60 * 1000
}

# Single service account for the pipeline (Airflow in Phase 13, CI later if ever needed) --
# project-level roles rather than per-resource bindings, a deliberate simplification for a
# single-purpose, disposable portfolio project (see docs/design-decisions.md). A real
# production system would scope these to the bucket/dataset level.
resource "google_service_account" "pipeline" {
  account_id   = "mdm-pipeline"
  display_name = "patient-dedup-system pipeline"
}

resource "google_project_iam_member" "pipeline_bigquery" {
  project = var.project_id
  role    = "roles/bigquery.dataEditor"
  member  = "serviceAccount:${google_service_account.pipeline.email}"
}

resource "google_project_iam_member" "pipeline_bigquery_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.pipeline.email}"
}

resource "google_project_iam_member" "pipeline_storage" {
  project = var.project_id
  role    = "roles/storage.objectAdmin"
  member  = "serviceAccount:${google_service_account.pipeline.email}"
}

# Phase 12: Dataproc Serverless batches need their own API enabled (unlike BigQuery/Storage,
# which come enabled by default on a new project) and a worker role for whichever service
# account actually runs the batch. `--service-account` on `gcloud dataproc batches submit`
# points at this same pipeline SA rather than the project's default Compute Engine SA, so
# the batch's runtime identity is the same least-privilege SA already scoped to exactly the
# BigQuery/GCS access the job needs -- one identity for the whole pipeline, not a second one
# with its own, wider default permissions.
resource "google_project_service" "dataproc" {
  project            = var.project_id
  service            = "dataproc.googleapis.com"
  disable_on_destroy = false # portfolio project: don't fight terraform destroy over an API flag
}

resource "google_project_iam_member" "pipeline_dataproc_worker" {
  project = var.project_id
  role    = "roles/dataproc.worker"
  member  = "serviceAccount:${google_service_account.pipeline.email}"
}

# The spark-bigquery-connector reads (and, for its "indirect" write path, reads back)
# through the BigQuery Storage API, which needs bigquery.readsessions.create -- a
# permission `bigquery.dataEditor`/`bigquery.jobUser` don't include (discovered the hard
# way: a real batch got all the way through scoring 398k pairs and only failed at the
# final write, see docs/design-decisions.md, Phase 12).
resource "google_project_iam_member" "pipeline_bigquery_read_session_user" {
  project = var.project_id
  role    = "roles/bigquery.readSessionUser"
  member  = "serviceAccount:${google_service_account.pipeline.email}"
}
