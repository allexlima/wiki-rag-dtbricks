# MediaWiki — Local Docker & AWS ECS Fargate

Two deployment modes for the **MediaWiki 1.42** instance backed by **Lakebase PostgreSQL**:

| Mode | Use case | Endpoint |
|------|----------|----------|
| **Local Docker** (default) | Development, quick demos | `http://localhost:8080` |
| **AWS ECS Fargate** (optional) | Public endpoint for Databricks jobs/apps | ALB DNS (auto-generated) |

---

## Local Docker (Default)

From the **project root**:

```bash
make setup-wiki         # Auto-generates .env from Databricks secrets, starts container
make demo-load          # Interactive dataset selector → loads pages + images
```

MediaWiki will be available at **http://localhost:8080**.

To tear down: `make wiki-destroy`

---

## AWS ECS Fargate (Optional)

Deploys MediaWiki as a Fargate service behind an ALB, giving you a public URL that Databricks notebooks and apps can reach.

> [!NOTE]
> This assumes you've already completed **Setup (one-time)** from the [root README](../README.md) (`make setup-secrets` + `make setup-lakebase`). The deploy script reads credentials directly from the Databricks secret scope — no `.env` needed.

### Prerequisites

| Tool | Install |
|------|---------|
| AWS CLI | `brew install awscli` or [docs](https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html) |
| AWS CDK | `npm install -g aws-cdk` |
| Docker | Required for building the container image |

### Profiles & Region

```bash
# AWS profile (if non-default)
export AWS_PROFILE=my-profile

# Databricks profile (if non-default)
export PROFILE=my-workspace

# Region defaults to us-east-1; override with:
export AWS_DEFAULT_REGION=eu-west-1
```

### Deploy

```bash
cd mediawiki/cdk
./deploy.sh
```

The script will:
1. Read Lakebase credentials from the Databricks secret scope
2. Sync them to AWS Secrets Manager
3. Pre-allocate a **NAT Elastic IP** and add it to the **Databricks workspace IP access list** (so Fargate can reach Lakebase from the first boot)
4. Ask whether to **restrict ALB access** to your current IP (`[Y/n]`)
5. Run `cdk deploy` (VPC, ECS Fargate, ALB)
6. Export `MEDIAWIKI_URL` for the current session

After deployment, load content:

```bash
# From project root — uses MEDIAWIKI_URL exported by deploy.sh
make demo-load
```

> [!IMPORTANT]
> To persist `MEDIAWIKI_URL` across terminals, add to your shell profile or `databricks.yml`:
> ```yaml
> variables:
>   mediawiki_url:
>     default: "http://<your-alb-dns-name>"
> ```

### Network & Security

| Resource | Purpose |
|----------|---------|
| **NAT Elastic IP** | Tagged `wiki-rag-nat` — reused across deploys. Stable outbound IP for Lakebase access. |
| **Databricks IP access list** | Entry `wiki-rag-ecs-nat` — auto-created with the NAT EIP. Allows Fargate → Lakebase. |
| **ALB security group** | Inbound HTTP (:80). Restricted to your IP by default. |

> [!TIP]
> **IP changed?** (VPN, travel, ISP) Re-run `./deploy.sh` — it detects your new IP and updates the security group.

> [!NOTE]
> **Manual `cdk deploy`** (without `deploy.sh`): the stack falls back to auto-creating the NAT EIP. You must manually add the NAT IP (from the `NatElasticIp` output) to the Databricks workspace IP access list.

### Destroy

```bash
cd mediawiki/cdk
cdk destroy
```

Clean up resources created outside CloudFormation:

```bash
# Release the pre-allocated NAT Elastic IP
EIP_ALLOC=$(aws ec2 describe-addresses --filters "Name=tag:Name,Values=wiki-rag-nat" \
    --query 'Addresses[0].AllocationId' --output text)
[ "$EIP_ALLOC" != "None" ] && aws ec2 release-address --allocation-id "$EIP_ALLOC"

# Remove Databricks workspace IP access list entry
# (Settings → Security → IP Access Lists → delete "wiki-rag-ecs-nat")

# Delete AWS Secrets Manager secret
aws secretsmanager delete-secret --secret-id wiki-rag/mediawiki --force-delete-without-recovery
```

---

## Adding Your Own Dataset

Create a folder under `mediawiki/dataset/`:

```
mediawiki/dataset/my-wiki/
├── Main_Page.md          # Index page (optional but recommended)
├── 01_topic.md           # Content pages (# Title on first line)
├── 02_topic.md
└── images/               # Referenced images (optional)
    ├── diagram.png
    └── photo.jpg
```

It will appear automatically in the `make demo-load` interactive selector.
