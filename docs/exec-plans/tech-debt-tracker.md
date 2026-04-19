# Tech Debt Tracker

| Item | Area | Priority | Added | Notes |
|------|------|----------|-------|-------|
| CI: machine-enforce Console browser-verification contract | Tooling | P0 | 2026-04-19 | Only mechanical gate closing the known loopholes in the doc-based process (see [CONSOLE_TASK_CHECKLIST.md](../CONSOLE_TASK_CHECKLIST.md)). Required checks: (1) any diff under `services/console/src/**` also modifies `docs/CONSOLE_BROWSER_TESTING.md`; (2) PR body or merge-commit trailer names the scenario numbers run in the browser (e.g., `Browser-Verified-Scenarios: 1, 2, 16`); (3) any new `⚠ gap` cell added to the coverage matrix has a matching row in this tracker. Without CI, the 3-doc process is aspirational — an agent can tick checkboxes without evidence. |
