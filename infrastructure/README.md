# AWS Infrastructure

This directory contains the AWS deployment assets for the Phase 1 Persistent Agent Runtime MVP.

## Prerequisites

- AWS CLI
- AWS Session Manager plugin
- AWS CDK CLI
- Docker
- Node.js 22+

## AWS credentials

Authenticate with an AWS account before running any deploy or destroy command:

```bash
aws sts get-caller-identity
```

## Required Secrets Manager secrets

These secrets must already exist in AWS Secrets Manager before deployment. Each secret must store a plaintext secret string only.

- `tavilySecretName` (optional): Tavily API key for the worker `web_search` tool
- `anthropicSecretName` (optional): Anthropic API key for Model Discovery
- `openaiSecretName` (optional): OpenAI API key for Model Discovery

## CDK context parameters

- `envName`: stack prefix, defaults to `dev`
- `workerDesiredCount`: fixed Fargate worker count, defaults to `1`
- `accessHostInstanceType`: access host size, defaults to `t3.micro`
- `tavilySecretName`: optional existing Tavily secret name or ARN
- `anthropicSecretName`: optional existing Anthropic secret name or ARN
- `openaiSecretName`: optional existing OpenAI secret name or ARN

## Commands

Run these from [`/Users/shenjianan/Project/persistent-agent-runtime/infrastructure/cdk`](/Users/shenjianan/Project/persistent-agent-runtime/infrastructure/cdk):

```bash
npm install
npm run build
npm test
npx cdk synth
npx cdk deploy --all
npx cdk deploy --all -c tavilySecretName=par/dev/tavily_api_key
npx cdk destroy --all
```

## Access workflow

The ALB is internal-only. Operators reach it through the SSM-managed access host with Session Manager remote-host port forwarding:

```bash
aws ssm start-session \
  --target <access-host-instance-id> \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters '{"host":["<internal-alb-dns>"],"portNumber":["80"],"localPortNumber":["8080"]}'
```

Then open `http://localhost:8080` in a browser. There is no application-layer password in Phase 1.

## Schema bootstrap

Schema migrations run through a Lambda-backed custom resource and are tracked in `schema_migrations`. Deployments apply only unapplied migration files.

## Model discovery

Model Discovery runs once automatically after schema bootstrap completes and then runs daily on an EventBridge schedule.

## Cost estimate

Approximate MVP dev cost:

- Aurora Serverless v2: ~$22/month
- NAT Gateway: ~$35/month
- ECS Fargate services: ~$53/month total for API, Console, Worker at the default sizes
- Internal ALB: ~$16/month
- CloudWatch Logs and Lambda: low single digits
- Access host: ~$8/month

Expect roughly `$135/month` before Secrets Manager charges and outbound API traffic.
