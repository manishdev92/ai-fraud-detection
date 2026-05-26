variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region for Cloud Run and bucket"
  type        = string
  default     = "us-central1"
}

variable "dataset_id" {
  description = "BigQuery dataset ID"
  type        = string
  default     = "fraud_investigation"
}

variable "bucket_name" {
  description = "Globally unique GCS bucket name for compliance reports"
  type        = string
}

variable "service_name" {
  description = "Cloud Run service name"
  type        = string
  default     = "fraud-investigation-api"
}

variable "image" {
  description = "Container image for Cloud Run"
  type        = string
  default     = "gcr.io/cloudrun/hello" # replace after docker push
}

variable "gemini_api_key" {
  description = "Gemini API key (sensitive — prefer Secret Manager in production)"
  type        = string
  sensitive   = true
  default     = ""
}
