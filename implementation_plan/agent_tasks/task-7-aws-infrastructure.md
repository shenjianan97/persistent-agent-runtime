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

**DEPLOYMENT ACCESS RULE:** If you reach the point of performing a real AWS deployment, bootstrap, destroy, or any command that requires live AWS account access, you MUST stop and ask the user for AWS credentials/configured account access first. Local CDK synthesis, unit tests, and static template validation may proceed without that prompt.

## Context
The Phase 1 Persistent Agent Runtime is designed as a cloud-native architecture relying heavily on AWS services. To ensure reliable, scalable, and secure deployment, we need to codify the infrastructure. The core dependencies include an Amazon Aurora Serverless v2 PostgreSQL cluster (acting as the queue and state store), and container execution environments (ECS Fargate) for the stateless API Service and the Python Worker Service.
Because the services are deployed on ECS Fargate, this task also owns the application containerization assets needed to build and publish runnable images for those services.

## Task-Specific Shared Contract
- Treat `PROJECT.md` and `design/PHASE1_DURABLE_EXECUTION.md` as the canonical infrastructure direction. This task should not reopen infrastructure choices already made.
- Infrastructure choices are fixed for Phase 1: AWS CDK in TypeScript, Aurora Serverless v2 PostgreSQL, ECS Fargate, private subnets, NAT for outbound provider access, and CloudWatch/OpenTelemetry integration.
- Schema initialization must remain decoupled from service startup.
- This task provides the deployment substrate only. It does not change application runtime contracts, schema semantics, or API behavior.
- This task must make container packaging explicit. Do not assume Dockerfiles, image build contexts, or image publication wiring will be provided elsewhere unless they already exist in the repo.

## Affected Component
- **Service/Module:** AWS Cloud Infrastructure
- **File paths (if known):** `infrastructure/cdk/`, `src/api-service/`, `src/worker-service/`
- **Change type:** new code

## Dependencies
- **Must complete first:** None (Can be built in parallel with code tasks, but coordinate with application owners if Docker build inputs or startup commands are unclear)
- **Provides output to:** Final deployment/integration pipelines
- **Shared interfaces/contracts:** AWS Security Groups, VPC endpoints, and IAM Role definitions dictating exactly what the API and Worker containers are allowed to access.

## Implementation Specification
Step 1: Bootstrap an AWS CDK project in TypeScript inside the `infrastructure/cdk/` directory, matching the project-level IaC decision in `PROJECT.md`.
Step 2: Define reproducible container build assets for the API Service and Worker Service. If Dockerfiles or `.dockerignore` files do not already exist, create them in the service directories using production-oriented defaults and explicit startup commands.
Step 3: Define the image publication strategy used by the infrastructure. The CDK stack must either build/publish Docker assets directly or wire services to ECR repositories through a documented image release workflow.
Step 4: Define the foundational network: A VPC with isolated private subnets for the database and worker nodes, and NAT Gateways for outbound LangGraph/LLM API calls.
Step 5: Define the Data Tier: Provision an Amazon Aurora Serverless v2 PostgreSQL cluster. Ensure it resides in the isolated subnets and is accessible only to the compute security groups. Store the master credentials securely in AWS Secrets Manager.
Step 6: Define the Compute Tier: Provision ECS Fargate services for both the API Service and Worker Service. Define their task definitions, service discovery/logging configuration, and required environment variables/secrets for database and provider access. Ensure the task definitions consume the container images from Step 3. Provision an Application Load Balancer (ALB) in public subnets for the API Service, with a target group routing to the Fargate service.
Step 7: Define IAM Execution Roles for the tasks following the Principle of Least Privilege, specifically granting access to Bedrock (if used), ECR/image-pull permissions as needed, and CloudWatch Logs.
Step 8: Define a clean mechanism for schema initialization that is decoupled from application startup, consistent with the rollout guidance in `implementation_plan/plan.md`.
Step 9: Configure ECS auto-scaling for the Worker Service based on queue depth (number of `queued` tasks). The API Service should scale on CPU/request metrics.
Step 10: Ensure all components are tagged correctly for billing (e.g. `Project: PersistentAgentRuntime`).

## Acceptance Criteria
The implementation is complete when:
- [ ] Valid Infrastructure as Code files are committed to the repository.
- [ ] The API Service and Worker Service each have explicit container build definitions suitable for ECS deployment.
- [ ] The infrastructure clearly defines how container images are built and supplied to ECS.
- [ ] A `README.md` in the `infrastructure/` folder explains how to synthesize, deploy, and destroy the stack.
- [ ] The infrastructure covers Network, Database, ECS Fargate compute, schema-bootstrap strategy, and Security (IAM/SGs).

## Testing Requirements
- **Unit tests:** Use tools like CDK `Assertions` to ensure the synthesized templates are valid.
- **Integration tests:** (Optional for this specific agent task, depending on AWS account access) Perform a real deployment to a sandbox AWS account to verify the stack creates successfully without circular dependencies, but only after explicitly requesting AWS credentials or confirmed account access from the user.

## Constraints and Guardrails
- **DO NOT** make the database publicly accessible.
- **DO NOT** hardcode database passwords or API keys in the code; strictly use Secrets Manager.
- **DO NOT** rely on ad hoc manual image builds with undocumented flags or unpublished local assumptions.
- Ensure the ECS Fargate tasks are configured to export logs automatically to CloudWatch.

## Assumptions / Open Questions for This Task
- None. Use AWS CDK with TypeScript and ECS Fargate, as already decided in `PROJECT.md`.

<!-- AGENT_TASK_END: task-7-aws-infrastructure.md -->
