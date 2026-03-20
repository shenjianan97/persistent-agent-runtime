import * as crypto from 'crypto';
import * as fs from 'fs';
import * as path from 'path';
import { SecretsManagerClient, GetSecretValueCommand } from '@aws-sdk/client-secrets-manager';
import { Client } from 'pg';

type CloudFormationEvent = {
  RequestType: 'Create' | 'Update' | 'Delete';
  PhysicalResourceId?: string;
  ResourceProperties: Record<string, string>;
  OldResourceProperties?: Record<string, string>;
};

type SecretPayload = {
  host: string;
  port: number | string;
  dbname: string;
  username: string;
  password: string;
};

type Migration = {
  filename: string;
  checksum: string;
  sql: string;
};

const secretArn = process.env.DB_CREDENTIALS_SECRET_ARN;
const secretsClient = new SecretsManagerClient({});

function loadMigrations(): Migration[] {
  const migrationsDir = path.join(__dirname, 'migrations');
  return fs
    .readdirSync(migrationsDir)
    .filter((file) => /^\d{4}_.*\.sql$/.test(file))
    .sort()
    .map((filename) => {
      const sql = fs.readFileSync(path.join(migrationsDir, filename), 'utf8');
      return {
        filename,
        checksum: crypto.createHash('sha256').update(sql).digest('hex'),
        sql,
      };
    });
}

async function loadDbCredentials(): Promise<SecretPayload> {
  if (!secretArn) {
    throw new Error('DB_CREDENTIALS_SECRET_ARN is required');
  }

  const response = await secretsClient.send(new GetSecretValueCommand({ SecretId: secretArn }));
  const secretString = response.SecretString ?? Buffer.from(response.SecretBinary ?? '').toString('utf8');
  if (!secretString) {
    throw new Error('Aurora credentials secret is empty');
  }

  const parsed = JSON.parse(secretString) as SecretPayload;
  for (const key of ['host', 'port', 'dbname', 'username', 'password'] as const) {
    if (parsed[key] === undefined || parsed[key] === null || parsed[key] === '') {
      throw new Error(`Aurora credentials secret is missing ${key}`);
    }
  }
  return parsed;
}

async function connectToDatabase() {
  const credentials = await loadDbCredentials();
  const client = new Client({
    host: credentials.host,
    port: Number(credentials.port),
    database: credentials.dbname,
    user: credentials.username,
    password: credentials.password,
  });
  await client.connect();
  return client;
}

async function ensureLedgerTable(client: Client): Promise<void> {
  await client.query(`
    CREATE TABLE IF NOT EXISTS schema_migrations (
      filename text primary key,
      checksum text not null,
      applied_at timestamptz not null default now()
    )
  `);
}

async function applyMigrations(client: Client, migrations: Migration[]): Promise<void> {
  await ensureLedgerTable(client);

  const ledgerResult = await client.query<{ filename: string; checksum: string }>(
    'SELECT filename, checksum FROM schema_migrations ORDER BY filename',
  );
  const ledger = new Map(ledgerResult.rows.map((row) => [row.filename, row.checksum]));

  for (const migration of migrations) {
    const appliedChecksum = ledger.get(migration.filename);
    if (appliedChecksum && appliedChecksum !== migration.checksum) {
      throw new Error(`Checksum mismatch for ${migration.filename}`);
    }
    if (appliedChecksum) {
      continue;
    }

    await client.query('BEGIN');
    try {
      await client.query(migration.sql);
      await client.query(
        'INSERT INTO schema_migrations (filename, checksum) VALUES ($1, $2)',
        [migration.filename, migration.checksum],
      );
      await client.query('COMMIT');
    } catch (error) {
      await client.query('ROLLBACK');
      throw error;
    }
  }
}

export async function handler(event: CloudFormationEvent): Promise<{ PhysicalResourceId: string }> {
  const physicalResourceId = event.PhysicalResourceId ?? 'schema-bootstrap';

  if (event.RequestType === 'Delete') {
    try {
      return { PhysicalResourceId: physicalResourceId };
    } catch {
      return { PhysicalResourceId: physicalResourceId };
    }
  }

  const client = await connectToDatabase();
  try {
    await applyMigrations(client, loadMigrations());
    return { PhysicalResourceId: physicalResourceId };
  } finally {
    await client.end();
  }
}
