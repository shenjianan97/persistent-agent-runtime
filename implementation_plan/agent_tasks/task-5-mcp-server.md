<!-- AGENT_TASK_START: task-5-mcp-server.md -->

# Task 5: Co-located MCP Server

## Agent Instructions
You are a software engineer implementing one module of a larger system.
Your scope is strictly limited to this task. Do not modify components outside
the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read the following context files to understand the system architecture and constraints:
1. `PROJECT.md` 
2. `design/PHASE1_DURABLE_EXECUTION.md`

**CRITICAL POST-WORK:** After completing this task, you MUST update the status of this task to "Done" in the `implementation_plan/progress.md` file.

## Context
Instead of native function calling, tool execution delegates across standardized Model Context Protocol (MCP) server structures abstracting interface implementations directly. Phase 1 targets deterministic read-only functions resolving safely natively mimicking external interface architectures properly.

## Task-Specific Shared Contract
- Treat `design/PHASE1_DURABLE_EXECUTION.md` as the canonical Phase 1 tool contract. Phase 1 tools are strictly read-only and idempotent by design.
- The MCP server must expose `web_search`, `read_url`, and `calculator` through `listTools` with stable argument schemas, because Task 2 validates `allowed_tools` against this interface and Task 6 dispatches through it.
- Do not introduce mutable tools, customer-provided tools, or Phase 2 BYOT concepts into this task.
- Tool outputs are untrusted data; keep tool implementations narrow and schema-validated.

## Affected Component
- **Service/Module:** Co-located MCP Server (Python)
- **File paths (if known):** `src/worker-service/tools/`
- **Change type:** new code

## Dependencies
- **Must complete first:** None
- **Provides output to:** Task 6
- **Shared interfaces/contracts:** Official Python MCP library standards exposing localized internal ports iteratively natively mapping capabilities securely.

## Implementation Specification
Step 1: Initialize an asynchronous MCP JSON-RPC router framework structuring the generic endpoints defining server tool interfaces programmatically reliably providing valid representations via `listTools`.
Step 2: Implement `web_search` wrapping API interactions safely interpreting isolated HTTP requests natively via Search APIs (like Tavily).
Step 3: Implement `read_url` integrating web extraction modules securely filtering DOM elements directly exporting pure textual markdown configurations flawlessly encapsulating limits securely.
Step 4: Implement `calculator` strictly asserting non-LLM calculation paths independently preventing injection frameworks comprehensively evaluating AST constraints effectively ensuring stability mapping transparently safely.

## Acceptance Criteria
The implementation is complete when:
- [ ] Internal interfaces seamlessly dispatch requests without global system delays transparently operating against valid schema limitations autonomously.
- [ ] Strict tool definitions represent parameter expectations distinctly avoiding open-ended execution dependencies comprehensively natively.

## Testing Requirements
- **Unit tests:** Assert mathematical parsing limitations inherently blocking unsafe syntax patterns immediately inherently via isolated fixtures proactively safely.
- **Integration tests:** Local server instantiation checking list representation correctness matching specific structural payloads successfully.

## Constraints and Guardrails
- Tools MUST remain read-only without stateful system alterations mapping Phase 1 boundaries strictly defensively natively transparently safely effectively.
- Favor deterministic schemas and bounded outputs over convenience. The tool contract here becomes part of both API validation and graph execution behavior.

## Assumptions / Open Questions for This Task
- None

<!-- AGENT_TASK_END: task-5-mcp-server.md -->
