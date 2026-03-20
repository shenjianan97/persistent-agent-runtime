import { App, Tags } from 'aws-cdk-lib';
import { ComputeStack } from '../lib/compute-stack';
import { DataStack } from '../lib/data-stack';
import { NetworkStack } from '../lib/network-stack';

function requiredContext(app: App, key: string, fallback: string): string {
  const value = app.node.tryGetContext(key);
  if (typeof value === 'string' && value.trim().length > 0) {
    return value.trim();
  }
  if (typeof value === 'number') {
    return String(value);
  }
  return fallback;
}

function optionalContext(app: App, key: string): string | undefined {
  const value = app.node.tryGetContext(key);
  if (typeof value === 'string') {
    const trimmed = value.trim();
    return trimmed.length > 0 ? trimmed : undefined;
  }
  return undefined;
}

const app = new App();

const envName = requiredContext(app, 'envName', 'dev');
const workerDesiredCount = Number(requiredContext(app, 'workerDesiredCount', '1'));
const accessHostInstanceType = requiredContext(app, 'accessHostInstanceType', 't3.micro');
const tavilySecretName = optionalContext(app, 'tavilySecretName');
const anthropicSecretName = optionalContext(app, 'anthropicSecretName');
const openaiSecretName = optionalContext(app, 'openaiSecretName');

Tags.of(app).add('Project', 'PersistentAgentRuntime');
Tags.of(app).add('Environment', envName);

const networkStack = new NetworkStack(app, `${envName}-network`, {
  envName,
  stackName: `PersistentAgentRuntime-${envName}-Network`,
});

const dataStack = new DataStack(app, `${envName}-data`, {
  envName,
  stackName: `PersistentAgentRuntime-${envName}-Data`,
  vpc: networkStack.vpc,
  dbSecurityGroup: networkStack.dbSecurityGroup,
  lambdaSecurityGroup: networkStack.lambdaSecurityGroup,
  privateWithEgressSubnets: networkStack.privateWithEgressSubnets,
  isolatedSubnets: networkStack.isolatedSubnets,
  tavilySecretName,
  anthropicSecretName,
  openaiSecretName,
});

new ComputeStack(app, `${envName}-compute`, {
  envName,
  stackName: `PersistentAgentRuntime-${envName}-Compute`,
  vpc: networkStack.vpc,
  securityGroups: {
    accessHost: networkStack.accessHostSecurityGroup,
    alb: networkStack.albSecurityGroup,
    api: networkStack.apiSecurityGroup,
    console: networkStack.consoleSecurityGroup,
    worker: networkStack.workerSecurityGroup,
    db: networkStack.dbSecurityGroup,
    lambda: networkStack.lambdaSecurityGroup,
  },
  database: {
    host: dataStack.cluster.clusterEndpoint.hostname,
    port: dataStack.cluster.clusterEndpoint.port,
    name: dataStack.databaseName,
    credentialsSecret: dataStack.databaseCredentialsSecret,
  },
  workerDesiredCount,
  accessHostInstanceType,
  tavilySecret: dataStack.tavilySecret,
  anthropicSecret: dataStack.anthropicSecret,
  openaiSecret: dataStack.openaiSecret,
  schemaBootstrapReady: dataStack.schemaBootstrapResource,
});
