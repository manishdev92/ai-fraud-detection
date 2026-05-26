# Free-tier friendly: BigQuery dataset, GCS bucket, Cloud Run only (no GKE).

resource "google_bigquery_dataset" "fraud" {
  dataset_id                 = var.dataset_id
  friendly_name              = "Fraud Investigation"
  description                = "Transactions, findings, and compliance reports"
  location                   = "US"
  delete_contents_on_destroy = true
}

resource "google_storage_bucket" "reports" {
  name                        = var.bucket_name
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true

  lifecycle_rule {
    condition {
      age = 90
    }
    action {
      type = "Delete"
    }
  }
}

resource "google_cloud_run_v2_service" "api" {
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }

    containers {
      image = var.image

      ports {
        container_port = 8080
      }

      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "BIGQUERY_DATASET_ID"
        value = var.dataset_id
      }
      env {
        name  = "GCS_BUCKET_NAME"
        value = google_storage_bucket.reports.name
      }
      env {
        name  = "USE_BIGQUERY"
        value = "true"
      }
      env {
        name  = "APP_ENV"
        value = "cloud"
      }
      env {
        name  = "DATABASE_URL"
        value = "sqlite:////tmp/fraud.db"
      }
      dynamic "env" {
        for_each = var.gemini_api_key != "" ? [1] : []
        content {
          name  = "GEMINI_API_KEY"
          value = var.gemini_api_key
        }
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }
    }

    service_account = google_service_account.run_sa.email
  }
}

resource "google_service_account" "run_sa" {
  account_id   = "fraud-api-run"
  display_name = "Fraud Investigation API (Cloud Run)"
}

resource "google_project_iam_member" "run_bq" {
  project = var.project_id
  role    = "roles/bigquery.dataEditor"
  member  = "serviceAccount:${google_service_account.run_sa.email}"
}

resource "google_project_iam_member" "run_bq_job" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.run_sa.email}"
}

resource "google_storage_bucket_iam_member" "run_gcs" {
  bucket = google_storage_bucket.reports.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.run_sa.email}"
}

resource "google_cloud_run_service_iam_member" "public_invoker" {
  location = google_cloud_run_v2_service.api.location
  service  = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
