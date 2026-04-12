# Agent Capabilities — Track 2: E2B Sandbox & File Input — Progress

| Task | Component | Status | Description |
|------|-----------|--------|-------------|
| Task 1 | DB Migration + Sandbox Config | Pending | `sandbox_id` column, `dead_letter_reason` extension, agent sandbox config validation |
| Task 2 | Sandbox Provisioner + Lifecycle | Pending | E2B SDK setup, provision/pause/resume/destroy |
| Task 3 | sandbox_exec Tool | Pending | Shell command execution in sandbox |
| Task 4 | sandbox_read_file + sandbox_write_file | Pending | File I/O tools in sandbox |
| Task 5 | sandbox_download Tool | Pending | Sandbox file → S3 output artifact |
| Task 6 | Multipart Submission + File Injection | Pending | File upload on submit, inject into sandbox at task start |
| Task 7 | Crash Recovery + Cost Tracking | Pending | Reconnect by sandbox_id, E2B cost integration |
| Task 8 | Console UI | Pending | File attachment on submit, sandbox config in agent form |
| Task 9 | Integration Tests | Pending | End-to-end sandbox + file input flow |

## Notes

- Task 1 is the entry point — all tasks depend on it.
- Task 2 (sandbox provisioner) unlocks Tasks 3-7 which can run in parallel.
- Task 8 (Console) can start after Task 1 (only needs sandbox config API, not runtime).
- Task 9 (Integration Tests) is the final task.
- **Prerequisite:** Track 1 (Output Artifact Storage) must be complete. Track 2 uses Track 1's S3 client, artifact service, and `task_artifacts` table.
