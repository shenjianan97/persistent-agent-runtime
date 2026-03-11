# Console

React SPA for monitoring and controlling the Persistent Agent Runtime. Dark-mode terminal aesthetic using IBM Plex Mono + Syne fonts, brutalist (zero border-radius) design.

## Features

- **Dashboard** — real-time system health (runtime, DB, workers, queue depth)
- **Task List** — browse all tasks with status/agent filters, links to detail view
- **Task Dispatcher** — submit tasks with agent config, model, tools, and execution params
- **Execution Telemetry** — live checkpoint timeline, worker handoff detection, per-step cost chart
- **Dead Letter Queue** — browse failed tasks, 1-click redrive

## Tech Stack

React 19, TypeScript, Vite 6, Tailwind CSS v4, TanStack Query v5, React Router v7, React Hook Form + Zod, Recharts 2, shadcn/ui (Radix)

## Setup

```bash
npm install
cp .env.example .env   # set VITE_API_BASE_URL (default: http://localhost:8080)
npm run dev             # listens on 0.0.0.0:5173 by default
```

## API Base URL

The console calls the Spring Boot API service at the URL set by `VITE_API_BASE_URL`. If unset, defaults to `http://localhost:8080`.

For remote development and SSH port forwarding, the Vite dev server binds to `0.0.0.0` by default. Override it with `VITE_DEV_HOST` if you need a different interface.

When `VITE_DEV_TASK_CONTROLS_ENABLED=true`, the submit form also exposes the dev-only `Dev Sleep` tool and allows short task timeouts for local recovery/timeout testing.

Ways to configure it (highest priority first):

1. **Inline env var** — overrides everything: `VITE_API_BASE_URL=https://api.example.com npm run dev`
2. **`.env.local`** — always loaded, gitignored. For personal overrides not committed to repo.
3. **`.env.[mode]`** — mode-specific: `.env.development` loads on `npm run dev`, `.env.production` loads on `npm run build`.
4. **`.env`** — base defaults, loaded in all modes.

Example multi-environment setup:
```
.env                → VITE_API_BASE_URL=http://localhost:8080
.env.local          → VITE_DEV_TASK_CONTROLS_ENABLED=true
.env.production     → VITE_API_BASE_URL=https://api.prod.example.com
```

`VITE_` prefix is required — Vite only exposes prefixed vars to client code. The value is embedded at build time, not at runtime.

## Build

```bash
npm run build           # outputs to dist/
```

## Structure

```
src/
├── api/            # Fetch client + payload mapping
├── components/ui/  # shadcn/ui primitives
├── features/       # Feature modules (dashboard, task-list, submit, task-detail, dead-letter)
├── layout/         # AppShell, Header, Sidebar
├── lib/            # Utilities (class merging, formatting)
├── types/          # TypeScript interfaces (maps to Java DTOs)
├── App.tsx         # Router + QueryClientProvider
└── main.tsx        # Entry point
```
