# Penumbra — Deployment Guide

Single-container deployment on AWS EC2 via Terraform. Auto-deploys on every push to `main`.

---

## Prerequisites

| What | Where to get it |
|---|---|
| AWS account | [aws.amazon.com](https://aws.amazon.com) |
| Terraform ≥ 1.6 | `brew install terraform` or [terraform.io](https://developer.hashicorp.com/terraform/install) |
| AWS CLI v2 | `brew install awscli` |
| Docker | `brew install docker` (for local builds only) |
| Polymarket API keys | Run `python scripts/setup_l2.py` locally first |
| Tavily API key | [tavily.com](https://tavily.com) — free tier works |
| Alchemy API key | [alchemy.com](https://alchemy.com) — free tier works |

---

## First-time setup

### 1. Configure AWS credentials locally

```bash
aws configure
# or use aws sso login / export AWS_PROFILE=...
```

### 2. Fill in Terraform secrets

```bash
cd terraform
cp secrets.tfvars.example secrets.tfvars
nano secrets.tfvars   # fill in all values
```

`secrets.tfvars` is gitignored — it never gets committed.

### 3. Apply infrastructure

```bash
cd terraform
terraform init
terraform apply -var-file=secrets.tfvars
```

Terraform provisions:
- **ECR** repository for Docker images
- **EC2 t3.micro** (Amazon Linux 2023) with Docker + SSM agent
- **EBS 20 GB data volume** (separate from root, `prevent_destroy = true` — survives instance replacement)
- **Elastic IP** — stable public address
- **CloudWatch log group** — 30-day retention
- **SSM Parameter Store** — all app secrets stored as KMS-encrypted SecureStrings
- **IAM roles** — EC2 instance role (ECR pull, SSM, Bedrock) + GitHub Actions OIDC role (ECR push, SSM RunCommand)

At the end of `terraform apply`, note the outputs:

```
dashboard_url          = "http://1.2.3.4:8000"
elastic_ip             = "1.2.3.4"
ec2_instance_id        = "i-0abc123..."
github_actions_role_arn = "arn:aws:iam::123456789012:role/penumbra-github-actions"
ssm_connect_command    = "aws ssm start-session --target i-0abc123..."
```

### 4. Configure GitHub repository variables

In your GitHub repo → **Settings → Secrets and Variables → Actions → Variables** (not Secrets), add:

| Variable | Value |
|---|---|
| `AWS_ROLE_ARN` | value of `github_actions_role_arn` from Terraform output |
| `INSTANCE_ID` | value of `ec2_instance_id` from Terraform output |

These are non-sensitive — they're IAM role ARNs and instance IDs, not credentials.

### 5. First deploy

Push to `main` (or trigger the workflow manually in GitHub Actions). The workflow:
1. Authenticates to AWS via GitHub OIDC — **no AWS credentials stored in GitHub**
2. Builds and pushes the Docker image to ECR
3. Sends an SSM Run Command to the EC2 instance to pull and restart the container

The instance already has the app running if it found an image in ECR at bootstrap. If not, the first CI push starts it.

---

## Operations

### View logs

```bash
# Live logs via CloudWatch (from anywhere)
aws logs tail /penumbra/sentinel --follow --region us-east-1

# Or SSH-free shell on the instance via SSM
aws ssm start-session --target i-0abc123... --region us-east-1

# On the instance:
docker compose -f /opt/penumbra/docker-compose.yml logs -f
```

### Check health

```bash
curl http://ELASTIC_IP:8000/api/health
# → {"status":"ok","db":"connected","uptime_seconds":...}

curl http://ELASTIC_IP:8000/api/budget
# → {"tier1":{"calls_used":0,"calls_limit":5000},...}
```

### Rotate secrets

Update the value in SSM Parameter Store (console or CLI), then run the refresh script on the instance:

```bash
aws ssm start-session --target i-0abc123...

# On the instance:
sudo /opt/penumbra/refresh-secrets.sh
docker compose -f /opt/penumbra/docker-compose.yml restart
```

### Manual redeploy (without a code push)

```bash
INSTANCE_ID=i-0abc123...
COMMAND_ID=$(aws ssm send-command \
  --instance-ids "$INSTANCE_ID" \
  --document-name "AWS-RunShellScript" \
  --parameters 'commands=["/opt/penumbra/deploy.sh"]' \
  --query "Command.CommandId" \
  --output text)
aws ssm get-command-invocation \
  --command-id "$COMMAND_ID" \
  --instance-id "$INSTANCE_ID" \
  --query "StandardOutputContent" \
  --output text
```

### Replace the EC2 instance (e.g., resize)

The DuckDB data lives on a separate EBS volume (`penumbra-data`) with `prevent_destroy = true`. You can safely destroy and recreate the instance:

```bash
terraform apply -var-file=secrets.tfvars -replace=aws_instance.penumbra
```

The new instance will re-attach the data volume and pull the latest image from ECR on boot.

---

## Architecture

```
                  GitHub Actions (OIDC)
                         │ push to main
                         ▼
                  ┌─────────────┐
                  │  Amazon ECR │  ← docker push
                  └──────┬──────┘
                         │ docker pull (via SSM RunCommand)
                         ▼
              ┌───────────────────────────┐
              │       EC2 t3.micro        │
              │  ┌─────────────────────┐  │
              │  │  Docker container   │  │
              │  │                     │  │
:8000 ────────┼─▶│  FastAPI + Dashboard│  │
              │  │  Ingester           │  │
              │  │  Scanner            │  │
              │  │  Judge (4 workers)  │  │
              │  └────────┬────────────┘  │
              │           │               │
              │    /dev/xvdf (EBS 20 GB)  │
              │    DuckDB database        │
              └───────────────────────────┘
                         │
              SSM Parameter Store (secrets)
              CloudWatch Logs (/penumbra/sentinel)
              AWS Bedrock (LLM inference)
```

## Cost estimate

| Item | Monthly |
|---|---|
| EC2 t3.micro | ~$8.50 |
| EBS 20 GB gp3 (data) + 20 GB gp3 (root) | ~$3.20 |
| Elastic IP (attached to running instance) | $0 |
| ECR (< 500 MB after lifecycle policy) | ~$0.05 |
| CloudWatch Logs (low volume) | ~$0.50 |
| AWS Bedrock Nova Lite (5k calls/day) | ~$3–8 |
| **Total** | **~$15–20/mo** |

No SSH key management, no secret rotation ceremony, no manual deploys — just `git push`.
