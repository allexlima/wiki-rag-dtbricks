# MediaWiki — Local Docker & AWS ECS Fargate

This directory contains everything needed to run a **MediaWiki 1.42** instance backed by **Lakebase PostgreSQL**. Two deployment modes are supported:

| Mode | Use case | Endpoint |
|------|----------|----------|
| **Local Docker** (default) | Development, quick demos | `http://localhost:8080` |
| **AWS ECS Fargate** (optional) | Public endpoint for Databricks jobs/apps | ALB DNS (auto-generated) |

---

## Local Docker (Default)

From the **project root**, run:

```bash
make setup-wiki         # Auto-generates .env from Databricks secrets, starts container
make demo-load          # Interactive dataset selector → loads pages + images
```

MediaWiki will be available at **http://localhost:8080**.

To tear down: `make wiki-destroy`

---

## AWS ECS Fargate (Optional)

Deploy MediaWiki as a Fargate service behind an Application Load Balancer, giving you a public URL that Databricks notebooks and apps can reach.

### Prerequisites

| Tool | Install |
|------|---------|
| Databricks CLI | `>= 0.236.0` — secrets must be populated (`make setup-secrets` + `make setup-lakebase`) |
| AWS CLI | `brew install awscli` or [docs](https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html) |
| AWS CDK | `npm install -g aws-cdk` |
| Python 3.11+ | Required for CDK app |
| Docker | Required for building the container image |

### Multiple AWS Profiles

If you have more than one AWS profile in `~/.aws/credentials`:

```bash
# List profiles
aws configure list-profiles

# Set the profile for the current session
export AWS_PROFILE=my-profile
```

All commands below (`deploy.sh`, `cdk deploy`, `cdk destroy`) will use the exported profile.

### Region

The default region is **us-east-1**. Override it with:

```bash
# Option 1: Environment variable
export AWS_DEFAULT_REGION=eu-west-1

# Option 2: CDK context parameter (at deploy time)
cdk deploy -c region=eu-west-1

# Option 3: The region from your AWS_PROFILE is used automatically
```

### Step 1: Ensure Databricks Secrets Exist

The deploy script reads Lakebase credentials **directly from the Databricks secret scope** (`wiki-rag`) — the same secrets populated by `make setup-secrets` + `make setup-lakebase`. No `.env` file needed.

```bash
# If you haven't already:
make setup-secrets      # One-time: prompts for password
make setup-lakebase     # Provisions Lakebase + stores host/port/db secrets
```

### Step 2: Bootstrap CDK (One-Time per Account/Region)

```bash
cd mediawiki/cdk
cdk bootstrap
```

### Step 3: Deploy

```bash
cd mediawiki/cdk
./deploy.sh

# With explicit profiles:
DATABRICKS_CONFIG_PROFILE=my-db AWS_PROFILE=my-aws ./deploy.sh
```

The script will:
1. Read Lakebase credentials from **Databricks Secrets** (`wiki-rag` scope)
2. Sync them to **AWS Secrets Manager** (`wiki-rag/mediawiki`)
3. Set up a Python venv and install CDK dependencies
4. Run `cdk deploy` to create the ECS Fargate service + ALB

> [!IMPORTANT]
> **After deployment, the CDK output will display `MediaWikiUrl`.**
>
> Copy this URL and either:
>
> 1. **Export it** for the current session:
>    ```bash
>    export MEDIAWIKI_URL=http://<your-alb-dns-name>
>    ```
>
> 2. **Or add it to `databricks.yml`** (for Databricks jobs):
>    ```yaml
>    variables:
>      mediawiki_url:
>        default: "http://<your-alb-dns-name>"
>    ```
>
> This is how the ingestion pipeline (`src/pipeline.py`) and shell scripts know where to find MediaWiki.

### Step 4: Ingest Data into ECS MediaWiki

```bash
# From project root
MEDIAWIKI_URL=http://<alb-dns-name> make demo-load
```

### Destroy

```bash
cd mediawiki/cdk
cdk destroy
```

To also remove the AWS Secrets Manager secret:

```bash
aws secretsmanager delete-secret --secret-id wiki-rag/mediawiki --force-delete-without-recovery
```

---

## Adding Your Own Dataset

Create a folder under `mediawiki/dataset/` with:

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
