<!-- AGENT_TASK_START: task-7-aws-infrastructure.md -->

# Task 7: AWS Cloud Infrastructure

## Agent Instructions
You are a software engineer or cloud architect implementing the Infrastructure as Code (IaC) for a larger system.
Your scope is strictly limited to this task. Do not modify the application code components outside
the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read the following context files to understand the system architecture and constraints:
1. `PROJECT.md` 
2. `design/PHASE1_DURABLE_EXECUTION.md`

**CRITICAL POST-WORK:** After completing this task, you MUST update the status of this task to "Done" in the `implementation_plan/progress.md` file.

## Context
The Phase 1 Persistent Agent Runtime is designed as a cloud-native architecture relying heavily on AWS services. To ensure reliable, scalable, and secure deployment, we need to codify the infrastructure. The core dependencies include an Amazon Aurora Serverless v2 PostgreSQL cluster (acting as the queue and state store), and container execution environments (ECS Fargate) for the stateless API Service and the Python Worker Service.

## Task-Specific Shared Contract
- Treat `PROJECT.md` and `design/PHASE1_DURABLE_EXECUTION.md` as the canonical infrastructure direction. This task should not reopen infrastructure choices already made.
- Infrastructure choices are fixed for Phase 1: AWS CDK in TypeScript, Aurora Serverless v2 PostgreSQL, ECS Fargate, private subnets, NAT for outbound provider access, and CloudWatch/OpenTelemetry integration.
- Schema initialization must remain decoupled from service startup.
- This task provides the deployment substrate only. It does not change application runtime contracts, schema semantics, or API behavior.

## Affected Component
- **Service/Module:** AWS Cloud Infrastructure
- **File paths (if known):** `infrastructure/cdk/`
- **Change type:** new code

## Dependencies
- **Must complete first:** None (Can be built in parallel with code tasks)
- **Provides output to:** Final deployment/integration pipelines
- **Shared interfaces/contracts:** AWS Security Groups, VPC endpoints, and IAM Role definitions dictating exactly what the API and Worker containers are allowed to access.

## Implementation Specification
Step 1: Bootstrap an AWS CDK project in TypeScript inside the `infrastructure/cdk/` directory, matching the project-level IaC decision in `PROJECT.md`.
Step 2: Define the foundational network: A VPC with isolated private subnets for the database and worker nodes, and NAT Gateways for outbound LangGraph/LLM API calls.
Step 3: Define the Data Tier: Provision an Amazon Aurora Serverless v2 PostgreSQL cluster. Ensure it resides in the isolated subnets and is accessible only to the compute security groups. Store the master credentials securely in AWS Secrets Manager.
Step 4: Define the Compute Tier: Provision ECS Fargate services for both the API Service and Worker Service. Define their task definitions, service discovery/logging configuration, and required environment variables/secrets for database and provider access. Provision an Application Load Balancer (ALB) in public subnets for the API Service, with a target group routing to the Fargate service.
Step 5: Define IAM Execution Roles for the tasks following the Principle of Least Privilege, specifically granting access to Bedrock (if used) and CloudWatch Logs.
Step 6: Define a clean mechanism for schema initialization that is decoupled from application startup, consistent with the rollout guidance in `implementation_plan/plan.md`.
Step 7: Configure ECS auto-scaling for the Worker Service based on queue depth (number of `queued` tasks). The API Service should scale on CPU/request metrics.
Step 8: Ensure all components are tagged correctly for billing (e.g. `Project: PersistentAgentRuntime`).

## Acceptance Criteria
The implementation is complete when:
- [ ] Valid Infrastructure as Code files are committed to the repository.
- [ ] A `README.md` in the `infrastructure/` folder explains how to synthesize, deploy, and destroy the stack.
- [ ] The infrastructure covers Network, Database, ECS Fargate compute, schema-bootstrap strategy, and Security (IAM/SGs).

## Testing Requirements
- **Unit tests:** Use tools like CDK `Assertions` to ensure the synthesized templates are valid.
- **Integration tests:** (Optional for this specific agent task, depending on AWS account access) Perform a real deployment to a sandbox AWS account to verify the stack creates successfully without circular dependencies.

## Constraints and Guardrails
- **DO NOT** make the database publicly accessible.
- **DO NOT** hardcode database passwords or API keys in the code; strictly use Secrets Manager.
- Ensure the ECS Fargate tasks are configured to export logs automatically to CloudWatch.

## Assumptions / Open Questions for This Task
- None. Use AWS CDK with TypeScript and ECS Fargate, as already decided in `PROJECT.md`.

<!-- AGENT_TASK_END: task-7-aws-infrastructure.md -->
