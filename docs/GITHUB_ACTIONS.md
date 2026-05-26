# Run GitHub Actions workflows

Repo: https://github.com/manishdev92/ai-fraud-detection/actions

## Workflows

| Workflow | File | When it runs |
|----------|------|----------------|
| **CI Tests** | `ci-tests.yml` | Every push/PR to `main`, or manual |
| **Deploy to AWS** | `deploy-aws.yml` | Push to `main` (app paths), or manual |

---

## 1. Run CI (no AWS required)

### Option A — GitHub UI

1. Open https://github.com/manishdev92/ai-fraud-detection/actions
2. Click **CI Tests** (left sidebar)
3. Click **Run workflow** → branch `main` → **Run workflow**

### Option B — CLI

```bash
gh workflow run ci-tests.yml --ref main
gh run watch --repo manishdev92/ai-fraud-detection
```

### Option C — Local (same steps as CI)

```bash
./scripts/run-ci-local.sh
```

---

## 2. Run Deploy to AWS (requires AWS infra first)

Deploy **will fail** until:

1. Terraform has been applied (`infra/aws/terraform`)
2. GitHub secret **`AWS_ROLE_ARN`** is set

### Step 2a — Terraform (one time)

```bash
cd infra/aws/terraform
cp terraform.tfvars.example terraform.tfvars
# Edit gemini_api_key in terraform.tfvars

terraform init
terraform apply
```

### Step 2b — GitHub secret

```bash
terraform output -raw github_actions_role_arn
```

Add in GitHub → **Settings → Secrets and variables → Actions → New repository secret**:

- Name: `AWS_ROLE_ARN`
- Value: (ARN from above)

### Step 2c — Trigger deploy

```bash
gh workflow run deploy-aws.yml --ref main
gh run watch --repo manishdev92/ai-fraud-detection
```

Or: **Actions → Deploy to AWS → Run workflow**

---

## First-time GitHub note

If the **Actions** tab shows *"Workflows aren’t being run on this repository"*, click **I understand my workflows, go ahead and enable them**.

---

## Troubleshooting

| Error | Fix |
|-------|-----|
| `AWS_ROLE_ARN is not set` | Complete Step 2b |
| `Could not assume role` | Re-run `terraform apply`; confirm repo name in `terraform.tfvars` matches GitHub |
| `CannotPullContainerError` | Run deploy once so ECR has an image |
| `describe-task-definition` not found | Run `terraform apply` first |
| No runs appear | Enable Actions on repo; push a commit or use **Run workflow** |
