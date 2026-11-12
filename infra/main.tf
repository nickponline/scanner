terraform {
  required_version = ">= 1.9"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 7.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
  backend "gcs" {
    bucket = "scanner-514023-tfstate"
    prefix = "infra"
  }
}

variable "project" { default = "scanner-514023" }
variable "region" { default = "us-central1" }

provider "google" {
  project               = var.project
  region                = var.region
  user_project_override = true
  billing_project       = var.project
}

data "google_project" "this" {}

locals {
  services = [
    "artifactregistry.googleapis.com", "batch.googleapis.com", "cloudbuild.googleapis.com",
    "compute.googleapis.com", "firestore.googleapis.com", "iam.googleapis.com",
    "iamcredentials.googleapis.com", "logging.googleapis.com", "monitoring.googleapis.com",
    "run.googleapis.com", "secretmanager.googleapis.com", "storage.googleapis.com",
    "workflows.googleapis.com", "workflowexecutions.googleapis.com",
    "generativelanguage.googleapis.com", "apikeys.googleapis.com", "cloudquotas.googleapis.com",
  ]
}

resource "google_project_service" "svc" {
  for_each           = toset(local.services)
  service            = each.value
  disable_on_destroy = false
}

# ---------- storage ----------
resource "google_storage_bucket" "data" {
  name                        = "${var.project}-data"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  cors {
    origin          = ["*"]
    method          = ["GET", "HEAD", "PUT", "POST"]
    response_header = ["Content-Type", "Content-Range", "Range", "Accept-Ranges", "Content-Length", "ETag", "x-goog-resumable", "Location"]
    max_age_seconds = 3600
  }
  lifecycle_rule {
    condition {
      matches_prefix = ["work/"]
      age            = 30
    }
    action { type = "Delete" }
  }
  depends_on = [google_project_service.svc]
}

resource "google_artifact_registry_repository" "scanner" {
  location      = var.region
  repository_id = "scanner"
  format        = "DOCKER"
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions { keep_count = 10 }
  }
  depends_on = [google_project_service.svc]
}

resource "google_firestore_database" "default" {
  name        = "(default)"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"
  depends_on  = [google_project_service.svc]
}

# ---------- service accounts ----------
resource "google_service_account" "api" {
  account_id   = "scanner-api"
  display_name = "Scanner API (Cloud Run)"
}

resource "google_service_account" "pipeline" {
  account_id   = "scanner-pipeline"
  display_name = "Scanner pipeline (Workflows + Batch VMs)"
}

locals {
  api_roles = [
    "roles/datastore.user", "roles/workflows.invoker", "roles/logging.logWriter",
  ]
  pipeline_roles = [
    "roles/datastore.user", "roles/logging.logWriter", "roles/batch.agentReporter",
    "roles/batch.jobsEditor", "roles/artifactregistry.reader", "roles/monitoring.metricWriter",
    "roles/workflows.invoker",
  ]
}

resource "google_project_iam_member" "api" {
  for_each = toset(local.api_roles)
  project  = var.project
  role     = each.value
  member   = "serviceAccount:${google_service_account.api.email}"
}

resource "google_project_iam_member" "pipeline" {
  for_each = toset(local.pipeline_roles)
  project  = var.project
  role     = each.value
  member   = "serviceAccount:${google_service_account.pipeline.email}"
}

resource "google_storage_bucket_iam_member" "api" {
  bucket = google_storage_bucket.data.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.api.email}"
}

resource "google_storage_bucket_iam_member" "pipeline" {
  bucket = google_storage_bucket.data.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.pipeline.email}"
}

# API signs GCS URLs via IAM signBlob on its own identity.
resource "google_service_account_iam_member" "api_self_sign" {
  service_account_id = google_service_account.api.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.api.email}"
}

# Workflows (running as pipeline SA) launches Batch jobs that run as the pipeline SA.
resource "google_service_account_iam_member" "pipeline_actas" {
  service_account_id = google_service_account.pipeline.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.pipeline.email}"
}

# API starts workflow executions that run as the pipeline SA.
resource "google_service_account_iam_member" "api_actas_pipeline" {
  service_account_id = google_service_account.pipeline.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.api.email}"
}

# ---------- secrets ----------
resource "random_password" "owner_token" {
  length  = 40
  special = false
}

resource "google_secret_manager_secret" "api_tokens" {
  secret_id = "api-tokens"
  replication {
    auto {}
  }
  depends_on = [google_project_service.svc]
}

resource "google_secret_manager_secret_version" "api_tokens" {
  secret      = google_secret_manager_secret.api_tokens.id
  secret_data = jsonencode({ (random_password.owner_token.result) = "nick@liminal.xyz" })
  lifecycle { ignore_changes = [secret_data] }
}

resource "random_password" "share_key" {
  length  = 48
  special = false
}

resource "google_secret_manager_secret" "share_key" {
  secret_id = "share-signing-key"
  replication {
    auto {}
  }
  depends_on = [google_project_service.svc]
}

resource "google_secret_manager_secret_version" "share_key" {
  secret      = google_secret_manager_secret.share_key.id
  secret_data = random_password.share_key.result
}

resource "google_apikeys_key" "gemini" {
  name         = "scanner-gemini"
  display_name = "Scanner pipeline Gemini key"
  restrictions {
    api_targets { service = "generativelanguage.googleapis.com" }
  }
  depends_on = [google_project_service.svc]
}

resource "google_secret_manager_secret" "gemini" {
  secret_id = "gemini-api-key"
  replication {
    auto {}
  }
  depends_on = [google_project_service.svc]
}

resource "google_secret_manager_secret_version" "gemini" {
  secret      = google_secret_manager_secret.gemini.id
  secret_data = google_apikeys_key.gemini.key_string
}

resource "google_secret_manager_secret_iam_member" "api_tokens" {
  for_each  = { tokens = google_secret_manager_secret.api_tokens.id, share = google_secret_manager_secret.share_key.id }
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}

resource "google_secret_manager_secret_iam_member" "pipeline_gemini" {
  secret_id = google_secret_manager_secret.gemini.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.pipeline.email}"
}

# ---------- workflow ----------
resource "google_workflows_workflow" "pipeline" {
  name            = "scanner-pipeline"
  region          = var.region
  service_account = google_service_account.pipeline.id
  source_contents = file("${path.module}/../pipeline/workflows/pipeline.yaml")
  call_log_level  = "LOG_ERRORS_ONLY"
  depends_on      = [google_project_service.svc]
}

output "bucket" { value = google_storage_bucket.data.name }
output "api_sa" { value = google_service_account.api.email }
output "pipeline_sa" { value = google_service_account.pipeline.email }
output "owner_token" {
  value     = random_password.owner_token.result
  sensitive = true
}
