import { spawn } from 'node:child_process';

const apiBaseUrl = process.env.VITE_API_BASE_URL || 'http://localhost:8080';
const devHost = process.env.VITE_DEV_HOST || '0.0.0.0';

console.log(`Console API endpoint: ${apiBaseUrl}`);
console.log(`Console dev host: ${devHost}`);

const child = spawn('vite', ['--host', devHost, ...process.argv.slice(2)], {
  stdio: 'inherit',
  env: process.env,
});

const forwardSignal = (signal) => {
  if (!child.killed) {
    child.kill(signal);
  }
};

process.on('SIGINT', () => forwardSignal('SIGINT'));
process.on('SIGTERM', () => forwardSignal('SIGTERM'));

child.on('exit', (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code ?? 0);
});
