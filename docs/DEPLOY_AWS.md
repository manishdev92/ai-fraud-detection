# Deploy to AWS (GitHub + ECS Fargate)

This guide deploys the **Agentic Financial Fraud Investigation Platform** to AWS using:

| Layer | Service |
|--------|---------|
| CI | GitHub Actions (`ci.yml`) |
| CD | GitHub Actions (`deploy-aws.yml`) + OIDC (no long-lived AWS keys) |
| Compute | **ECS Fargate** |
| Registry | **ECR** |
| Load balancer | **ALB** (HTTP :80) |
| Database | **RDS PostgreSQL** (replaces ephemeral SQLite) |
| Secrets | **Secrets Manager** (`DATABASE_URL`, `GEMINI_API_KEY`) |
| Logs | **CloudWatch** |

Repo: https://github.com/manishdev92/ai-fraud-detection

---

## Architecture

```text
GitHub (push main) ──OIDC──► IAM Role ──► ECR push + ECS deploy
                                    │
Internet ──► ALB :80 ──► ECS Fargate :8080 ──► RDS PostgreSQL (private)
                                    │
                                    └──► Secrets Manager (DB URL, Gemini key)
                                    └──► Gemini API (egress)
```

**Estimated cost (us-east-1, 1 task):** ~$35–55/month (RDS micro + Fargate + ALB). Tear down when not demoing.

---

## Prerequisites

1. **AWS account** with admin access (or PowerUser + IAM)
2. **AWS CLI** v2 configured: `aws configure`
3. **Terraform** ≥ 1.5: `brew install terraform`
4. **GitHub repo** with this code pushed to `main`
5. **Gemini API key** (optional; template reports work without it)

---

## Step 1 — Provision infrastructure (Terraform)

```bash
cd infra/aws/terraform
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars — set gemini_api_key (or update secret later)
```

```bash
terraform init
terraform plan
terraform apply
```

Save outputs:

```bash
terraform output alb_url
terraform output github_actions_role_arn
terraform output ecr_repository_url
```

**First apply:** ECS may show unhealthy until the first Docker image is pushed (Step 3).

---

## Step 2 — Configure GitHub secrets

In GitHub → **Settings → Secrets and variables → Actions → Secrets**:

| Secret | Value |
|--------|--------|
| `AWS_ROLE_ARN` | `terraform output -raw github_actions_role_arn` |

Optional **Variables** (defaults match Terraform naming):

| Variable | Default |
|----------|---------|
| `AWS_REGION` | `us-east-1` |
| `ECS_CLUSTER_NAME` | `fraud-platform-prod-cluster` |
| `ECS_SERVICE_NAME` | `fraud-platform-prod-service` |
| `ECS_TASK_FAMILY` | `fraud-platform-prod-api` |

---

## Step 3 — Deploy application (GitHub Actions)

1. Push to `main`, or run **Actions → Deploy to AWS → Run workflow**.
2. Workflow builds Docker image, pushes to ECR, registers new ECS task definition, waits for service stable.
3. Open **`terraform output alb_url`** + `/docs` (e.g. `http://<alb-dns>/docs`).

### Verify

```bash
ALB=$(cd infra/aws/terraform && terraform output -raw alb_dns_name)
curl -s "http://${ALB}/health" | jq .
curl -s -X POST "http://${ALB}/generate-transactions" \
  -H "Content-Type: application/json" \
  -d '{"count": 200, "seed": 42}'
curl -s -X POST "http://${ALB}/run-fraud-investigation" \
  -H "Content-Type: application/json" \
  -d '{"lookback_hours": 336, "generate_report": true}' | jq .
```

---

## Step 4 — Update Gemini key (if not set in Terraform)

```bash
aws secretsmanager put-secret-value \
  --secret-id fraud-platform-prod/gemini-api-key \
  --secret-string "YOUR_GEMINI_API_KEY"

aws ecs update-service \
  --cluster fraud-platform-prod-cluster \
  --service fraud-platform-prod-service \
  --force-new-deployment
```

---

## Local Docker test (before AWS)

```bash
docker build -t fraud-platform-api:local .
docker run --rm -p 8080:8080 \
  -e DATABASE_URL=sqlite:///./data/fraud_platform.db \
  -e GEMINI_API_KEY= \
  -e USE_ADK=true \
  fraud-platform-api:local
curl http://localhost:8080/health
```

---

## HTTPS (production hardening)

1. Request **ACM certificate** for your domain.
2. Add **HTTPS listener** on ALB (443) and redirect HTTP → HTTPS.
3. Restrict ALB security group to corporate IP or CloudFront.

---

## Tear down

```bash
cd infra/aws/terraform
terraform destroy
```

Also delete ECR images if `force_delete` was not used.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| ECS tasks keep restarting | Check CloudWatch log group `/ecs/fraud-platform-prod`; often DB not ready or bad `DATABASE_URL` secret |
| `CannotPullContainerError` | Run deploy workflow once to push image to ECR |
| GitHub OIDC `Not authorized` | Confirm `AWS_ROLE_ARN` secret and repo name match `github_org` / `github_repo` in `terraform.tfvars` |
| Gemini always template | Set secret `fraud-platform-prod/gemini-api-key` and redeploy ECS |
| Health check failing | ALB path `/health` must return 200; allow 60s `startPeriod` on new tasks |

---

## GCP vs AWS in this project

| Capability | Local / GCP path | AWS path |
|------------|------------------|----------|
| OLTP DB | SQLite | **RDS PostgreSQL** |
| Analytics warehouse | BigQuery | (optional) Athena + S3 — not wired in v1 |
| Reports storage | GCS | (optional) S3 — not wired in v1 |
| Compute | Cloud Run | **ECS Fargate** |
| CI/CD | — | **GitHub Actions** |

Phase 1 fraud logic, ADK agents, and REST API are unchanged; only infrastructure differs.
