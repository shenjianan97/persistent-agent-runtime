# AWS Infrastructure

This directory contains the AWS CDK infrastructure and deployment assets for the Phase 1 Persistent Agent Runtime.

## Prerequisites

- [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
- [AWS Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html)
- [AWS CDK CLI](https://docs.aws.amazon.com/cdk/v2/guide/getting-started.html) (`npm install -g aws-cdk`)
- Docker (must be running)
- Node.js 22+

## Deployment Walkthrough

### 1. Configure and verify AWS credentials

CDK deploys to whichever account and region your AWS CLI is configured for. Set these up via `~/.aws/credentials` and `~/.aws/config`, environment variables, or SSO:

```bash
# Option A: default profile in ~/.aws/credentials
aws configure

# Option B: environment variables
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_REGION=us-west-2

# Option C: named profile
export AWS_PROFILE=my-profile
```

Verify your identity and target region:

```bash
aws sts get-caller-identity
aws configure get region
```

All resources (VPC, Aurora, ECS, etc.) are created in the configured region. The CDK bootstrap (step 3) is also region-specific.

### 2. Create Secrets Manager secrets (optional but recommended)

Without API key secrets, the system deploys but has no LLM models available. Each secret must be a **plaintext string** containing only the API key — not JSON.

```bash
# Anthropic (enables Claude models)
aws secretsmanager create-secret --name par/dev/anthropic_api_key --secret-string "sk-ant-..."

# OpenAI (enables GPT models)
aws secretsmanager create-secret --name par/dev/openai_api_key --secret-string "sk-proj-..."

# Tavily (enables web_search tool — optional)
aws secretsmanager create-secret --name par/dev/tavily_api_key --secret-string "tvly-..."
```

The secret names are arbitrary — they just need to match what you pass via CDK context.

### 3. Bootstrap CDK (first time only)

```bash
cd infrastructure/cdk
npx cdk bootstrap
```

### 4. Install, build, and test

```bash
cd infrastructure/cdk
npm install
npm run build
npm test
```

### 5. Synthesize (validates templates without deploying)

```bash
npx cdk synth \
  -c anthropicSecretName=par/dev/anthropic_api_key \
  -c openaiSecretName=par/dev/openai_api_key \
  -c tavilySecretName=par/dev/tavily_api_key
```

### 6. Deploy

```bash
npx cdk deploy --all --require-approval never \
  -c anthropicSecretName=par/dev/anthropic_api_key \
  -c openaiSecretName=par/dev/openai_api_key \
  -c tavilySecretName=par/dev/tavily_api_key
```

This creates three CloudFormation stacks:

| Stack | Resources |
|-------|-----------|
| `PersistentAgentRuntime-dev-Network` | VPC, subnets, NAT gateway, security groups |
| `PersistentAgentRuntime-dev-Data` | Aurora Serverless v2, schema bootstrap Lambda |
| `PersistentAgentRuntime-dev-Compute` | ECS cluster, ALB, API/Console/Worker services, access host, Model Discovery Lambda |

Deployment takes ~15-20 minutes. Docker images are built locally (cross-compiled to `linux/amd64` for Fargate) and pushed to ECR.

After deployment, the outputs include:
- `AccessHostInstanceId` — EC2 instance ID for SSM port forwarding
- `InternalAlbDnsName` — internal ALB DNS name

### 7. Tear down

```bash
npx cdk destroy --all \
  -c anthropicSecretName=par/dev/anthropic_api_key \
  -c openaiSecretName=par/dev/openai_api_key \
  -c tavilySecretName=par/dev/tavily_api_key
```

Note: Secrets Manager secrets are **not** deleted by `cdk destroy`. Delete them manually if needed:

```bash
aws secretsmanager delete-secret --secret-id par/dev/anthropic_api_key --force-delete-without-recovery
aws secretsmanager delete-secret --secret-id par/dev/openai_api_key --force-delete-without-recovery
aws secretsmanager delete-secret --secret-id par/dev/tavily_api_key --force-delete-without-recovery
```

## CDK Context Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `envName` | `dev` | Stack name prefix and resource naming |
| `workerDesiredCount` | `1` | Fixed Fargate worker task count |
| `accessHostInstanceType` | `t3.micro` | SSM-managed access host instance type |
| `tavilySecretName` | _(empty)_ | Existing Secrets Manager secret name for Tavily API key |
| `anthropicSecretName` | _(empty)_ | Existing Secrets Manager secret name for Anthropic API key |
| `openaiSecretName` | _(empty)_ | Existing Secrets Manager secret name for OpenAI API key |

## Access Workflow

The ALB is **internal-only**. Operators access the application via SSM Session Manager port forwarding through the access host:

```bash
aws ssm start-session \
  --target <AccessHostInstanceId> \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters '{"host":["<InternalAlbDnsName>"],"portNumber":["80"],"localPortNumber":["8080"]}'
```

Then open `http://localhost:8080` in a browser.

- Console UI is served at `/`
- API is available at `/v1/*`
- No application-layer password in Phase 1

The access host runs in a public subnet with SSM agent only — no inbound SSH or HTTP exposure.

## Schema Bootstrap

Schema migrations run automatically during deployment via a Lambda-backed CloudFormation custom resource. Migrations are tracked in a `schema_migrations` ledger table — only unapplied migrations are executed, and checksum mismatches on previously applied files will fail the deployment.

## Model Discovery

Model Discovery runs as a Lambda function that:
- Executes **once automatically** after schema bootstrap completes during deployment
- Runs **daily** via an EventBridge schedule thereafter
- Discovers available models from configured provider API keys
- Populates the `models` and `provider_keys` tables
- No-ops gracefully if no provider secrets are configured

## Architecture

```
Internet
    │
    ├── SSM Session Manager
    │       │
    │   ┌───▼───────────────┐
    │   │  Access Host      │  (public subnet, no inbound ports)
    │   └───┬───────────────┘
    │       │ port-forward
    │   ┌───▼───────────────┐
    │   │  Internal ALB     │  (private subnet)
    │   │  ├── /v1/* → API  │
    │   │  └── /*   → SPA   │
    │   └───┬───────┬───────┘
    │       │       │
    │   ┌───▼──┐ ┌──▼─────┐  ┌──────────┐
    │   │ API  │ │Console │  │ Worker   │  (private subnets, Fargate)
    │   └──┬───┘ └────────┘  └────┬─────┘
    │      │                      │
    │   ┌──▼──────────────────────▼──┐
    │   │  Aurora Serverless v2      │  (isolated subnets)
    │   └────────────────────────────┘
```

## Cost Estimate (MVP / dev)

| Resource | Monthly Cost (approx) |
|----------|-----------------------|
| Aurora Serverless v2 (0.5 ACU min, mostly idle) | ~$22 |
| NAT Gateway (1x) + data processing | ~$35 |
| ECS Fargate — API (0.5 vCPU, 1 GB) | ~$15 |
| ECS Fargate — Console (0.25 vCPU, 0.5 GB) | ~$8 |
| ECS Fargate — Worker (1 vCPU, 2 GB) | ~$30 |
| Internal ALB | ~$16 |
| CloudWatch Logs + Lambda | ~$5 |
| EC2 access host (t3.micro) | ~$8 |
| **Total** | **~$135/month** |

Plus Secrets Manager charges and outbound API traffic to LLM providers.
