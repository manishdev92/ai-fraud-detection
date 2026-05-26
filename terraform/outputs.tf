output "bigquery_dataset" {
  value = google_bigquery_dataset.fraud.dataset_id
}

output "gcs_bucket" {
  value = google_storage_bucket.reports.name
}

output "cloud_run_url" {
  value = google_cloud_run_v2_service.api.uri
}

output "service_account" {
  value = google_service_account.run_sa.email
}
