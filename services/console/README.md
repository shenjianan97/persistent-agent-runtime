# Persistent Agent Runtime - Console

The Console is a production-grade React Single Page Application (SPA) providing a utilitarian, terminal-inspired dashboard for monitoring and interacting with the Persistent Agent Runtime. It proves out durable execution concepts by providing visibility into step-by-step agent checkpoints, worker handoffs, precise cost aggregation, and dead-letter queue management.

## Aesthetic & Design
The console adheres to a strict "Industrial Terminal Dashboard" design system:
- **Dark Mode Only**: Built on deep charcoal and void black backgrounds.
- **Typography:** Driven by `IBM Plex Mono` for structural and data elements, with `Syne` for large metric displays.
- **Accents:** High-contrast, highly-saturated alerts (CRT Cyan for active, Acid Green for completion, Warning Amber for queued, Alert Red for failures).
- **Brutalism:** Pure geometric forms with zero border-radius (`rounded-none`). 

## Key Features

- **Dashboard Overview**: Aggregated view of system health and active workers.
- **Task Dispatcher**: A structured form to submit new directives, configuring agent parameters, LLM model choice, and allowed tools.
- **Execution Telemetry (Task Detail)**:
  - **Live Timeline**: An automatically scrolling, polling timeline of execution checkpoints.
  - **Worker Tracking**: Visual indicators when a task is handed off from one worker node to another between checkpoints.
  - **Financial Visibility**: Real-time micro-dollar to USD cost aggregation visualized with Recharts bar charts.
- **Dead Letter Queue (DLQ)**: A dedicated queue for tasks that have fatally failed or exhausted their retry limits, featuring rapid 1-click "Redrive" capabilities.

## Tech Stack

- **Core**: React 19, TypeScript
- **Bundler**: Vite 6
- **Styling**: Tailwind CSS v4
- **Components**: Customized [shadcn/ui](https://ui.shadcn.com/) (Radix UI primitives)
- **State & Data Fetching**: TanStack Query v5 (React Query)
- **Routing**: React Router v7
- **Forms**: React Hook Form + Zod validation
- **Charts**: Recharts 2
- **Icons**: Lucide React

## Local Development Setup

### Prerequisites
- Node.js (v20+ recommended)
- npm (v10+)

### Installation
From the `services/console` directory, install the project dependencies:

```bash
npm install
```

### Environment Configuration
Copy the `.env.example` file to create your local `.env` configuration:

```bash
cp .env.example .env
```
Ensure `VITE_API_BASE_URL` points to your running instance of the Spring Boot API Service (typically `http://localhost:8080`).

### Running the Development Server
Start the Vite development server:

```bash
npm run dev
```

The console will be accessible at [http://localhost:5173](http://localhost:5173).

## Building for Production

To create an optimized production build:

```bash
npm run build
```

This will run the TypeScript compiler (`tsc`) and bundle the application into the `dist/` directory, ready to be served by any static file host or CDN.

## Project Structure

```text
src/
├── api/            # Base API fetch client and payload mapping
├── components/     # Reusable UI primitives (shadcn overrides)
├── features/       # Feature-driven module directories
│   ├── dashboard/  # Health overview
│   ├── dead-letter/# DLQ management and redrive actions
│   ├── submit/     # Task creation forms and validation schemas
│   └── task-detail/# Live telemetry, charts, and execution logs
├── layout/         # App Shell (Sidebar, Header)
├── lib/            # Utility functions (e.g., Tailwind class merging)
├── types/          # TypeScript interfaces mapping to Java backend DTOs
├── App.tsx         # Root Router and QueryClientProvider
└── main.tsx        # React DOM mounting
```
