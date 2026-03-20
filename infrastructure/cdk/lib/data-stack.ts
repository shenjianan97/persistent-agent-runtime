import * as crypto from 'crypto';
import * as fs from 'fs';
import * as path from 'path';
import {
  Duration,
  CustomResource,
  RemovalPolicy,
  Stack,
  StackProps,
  aws_ec2 as ec2,
  aws_iam as iam,
  aws_lambda as lambda,
  aws_logs as logs,
  aws_rds as rds,
  aws_secretsmanager as secretsmanager,
  custom_resources as cr,
  CfnOutput,
} from 'aws-cdk-lib';
import { NodejsFunction, OutputFormat } from 'aws-cdk-lib/aws-lambda-nodejs';
import { Construct } from 'constructs';

export interface DataStackProps extends StackProps {
  envName: string;
  vpc: ec2.IVpc;
  dbSecurityGroup: ec2.ISecurityGroup;
  lambdaSecurityGroup: ec2.ISecurityGroup;
  privateWithEgressSubnets: ec2.ISubnet[];
  isolatedSubnets: ec2.ISubnet[];
  tavilySecretName?: string;
  anthropicSecretName?: string;
  openaiSecretName?: string;
}

interface ImportedSecretReference {
  readonly secret?: secretsmanager.ISecret;
  readonly arn?: string;
}

interface MigrationFile {
  readonly filename: string;
  readonly checksum: string;
  readonly sql: string;
}

function secretFromIdentifier(scope: Construct, id: string, identifier?: string): ImportedSecretReference {
  if (!identifier) {
    return {};
  }
  const secret = identifier.startsWith('arn:')
    ? secretsmanager.Secret.fromSecretCompleteArn(scope, id, identifier)
    : secretsmanager.Secret.fromSecretNameV2(scope, id, identifier);
  return { secret, arn: secret.secretArn };
}

function loadMigrationFiles(): MigrationFile[] {
  const migrationsDir = path.join(__dirname, 'schema-bootstrap', 'migrations');
  return fs
    .readdirSync(migrationsDir)
    .filter((file) => /^\d{4}_.*\.sql$/.test(file))
    .sort()
    .map((filename) => {
      const sql = fs.readFileSync(path.join(migrationsDir, filename), 'utf8');
      const checksum = crypto.createHash('sha256').update(sql).digest('hex');
      return { filename, checksum, sql };
    });
}

export class DataStack extends Stack {
  public readonly cluster: rds.DatabaseCluster;
  public readonly databaseName: string;
  public readonly databaseCredentialsSecret: secretsmanager.ISecret;
  public readonly tavilySecret?: secretsmanager.ISecret;
  public readonly anthropicSecret?: secretsmanager.ISecret;
  public readonly openaiSecret?: secretsmanager.ISecret;
  public readonly schemaBootstrapFunction: lambda.Function;
  public readonly schemaBootstrapProvider: cr.Provider;
  public readonly schemaBootstrapResource: CustomResource;
  public readonly migrations: MigrationFile[];

  constructor(scope: Construct, id: string, props: DataStackProps) {
    super(scope, id, props);

    this.databaseName = 'persistent_agent_runtime';

    this.tavilySecret = secretFromIdentifier(this, 'TavilySecret', props.tavilySecretName).secret;
    this.anthropicSecret = secretFromIdentifier(this, 'AnthropicSecret', props.anthropicSecretName).secret;
    this.openaiSecret = secretFromIdentifier(this, 'OpenAISecret', props.openaiSecretName).secret;
    this.migrations = loadMigrationFiles();

    this.cluster = new rds.DatabaseCluster(this, 'AuroraCluster', {
      engine: rds.DatabaseClusterEngine.auroraPostgres({
        version: rds.AuroraPostgresEngineVersion.VER_16_4,
      }),
      writer: rds.ClusterInstance.serverlessV2('writer'),
      vpc: props.vpc,
      vpcSubnets: { subnets: props.isolatedSubnets },
      storageEncrypted: true,
      credentials: rds.Credentials.fromGeneratedSecret('clusteradmin'),
      defaultDatabaseName: this.databaseName,
      serverlessV2MinCapacity: 0.5,
      serverlessV2MaxCapacity: 4,
      backup: {
        retention: Duration.days(7),
      },
      removalPolicy: RemovalPolicy.DESTROY,
      deletionProtection: false,
      securityGroups: [props.dbSecurityGroup],
      cloudwatchLogsExports: ['postgresql'],
    });

    this.databaseCredentialsSecret = this.cluster.secret!;

    const schemaBootstrapAssetPath = path.join(__dirname, 'schema-bootstrap');
    this.schemaBootstrapFunction = new NodejsFunction(this, 'SchemaBootstrapFunction', {
      entry: path.join(schemaBootstrapAssetPath, 'handler.ts'),
      runtime: lambda.Runtime.NODEJS_20_X,
      timeout: Duration.minutes(5),
      memorySize: 512,
      architecture: lambda.Architecture.X86_64,
      logRetention: logs.RetentionDays.ONE_WEEK,
      environment: {
        DB_CREDENTIALS_SECRET_ARN: this.databaseCredentialsSecret.secretArn,
      },
      vpc: props.vpc,
      vpcSubnets: { subnets: props.privateWithEgressSubnets },
      securityGroups: [props.lambdaSecurityGroup],
      bundling: {
        format: OutputFormat.CJS,
        target: 'node20',
        sourceMap: true,
        minify: false,
        loader: {
          '.sql': 'text',
        },
        commandHooks: {
          beforeBundling() {
            return [];
          },
          beforeInstall() {
            return [];
          },
          afterBundling() {
            return [];
          },
        },
      },
    });

    this.databaseCredentialsSecret.grantRead(this.schemaBootstrapFunction);
    this.cluster.grantConnect(this.schemaBootstrapFunction, 'clusteradmin');

    this.schemaBootstrapProvider = new cr.Provider(this, 'SchemaBootstrapProvider', {
      onEventHandler: this.schemaBootstrapFunction,
    });

    this.schemaBootstrapResource = new CustomResource(this, 'SchemaBootstrapResource', {
      serviceToken: this.schemaBootstrapProvider.serviceToken,
      properties: {
        DatabaseName: this.databaseName,
        MigrationsChecksum: this.migrations.map((migration) => `${migration.filename}:${migration.checksum}`).join('|'),
        MigrationCount: this.migrations.length,
      },
    });
    this.schemaBootstrapResource.node.addDependency(this.cluster);

    new CfnOutput(this, 'AuroraEndpoint', {
      value: this.cluster.clusterEndpoint.hostname,
    });
    new CfnOutput(this, 'AuroraPort', {
      value: this.cluster.clusterEndpoint.port.toString(),
    });
    new CfnOutput(this, 'DatabaseName', {
      value: this.databaseName,
    });
    new CfnOutput(this, 'AuroraCredentialsSecretArn', {
      value: this.databaseCredentialsSecret.secretArn,
    });
    if (this.tavilySecret) {
      new CfnOutput(this, 'TavilySecretArn', {
        value: this.tavilySecret.secretArn,
      });
    }
    if (this.anthropicSecret) {
      new CfnOutput(this, 'AnthropicSecretArn', {
        value: this.anthropicSecret.secretArn,
      });
    }
    if (this.openaiSecret) {
      new CfnOutput(this, 'OpenAISecretArn', {
        value: this.openaiSecret.secretArn,
      });
    }
  }
}
