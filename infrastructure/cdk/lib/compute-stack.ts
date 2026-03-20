import * as path from 'path';

import {
  CfnOutput,
  CustomResource,
  Duration,
  RemovalPolicy,
  Stack,
  StackProps,
  aws_ec2 as ec2,
  aws_ecs as ecs,
  aws_ecr_assets as ecrAssets,
  aws_elasticloadbalancingv2 as elbv2,
  aws_events as events,
  aws_events_targets as eventsTargets,
  aws_iam as iam,
  aws_lambda as lambda,
  aws_logs as logs,
  aws_secretsmanager as secretsmanager,
  custom_resources as cr,
} from 'aws-cdk-lib';
import { Construct } from 'constructs';

export interface ComputeSecurityGroups {
  readonly accessHost: ec2.ISecurityGroup;
  readonly alb: ec2.ISecurityGroup;
  readonly api: ec2.ISecurityGroup;
  readonly console: ec2.ISecurityGroup;
  readonly worker: ec2.ISecurityGroup;
  readonly db: ec2.ISecurityGroup;
  readonly lambda: ec2.ISecurityGroup;
}

export interface ComputeDatabaseConfig {
  readonly host: string;
  readonly port: number;
  readonly name: string;
  readonly credentialsSecret: secretsmanager.ISecret;
}

export interface ComputeStackProps extends StackProps {
  readonly envName: string;
  readonly vpc: ec2.IVpc;
  readonly securityGroups: ComputeSecurityGroups;
  readonly database: ComputeDatabaseConfig;
  readonly workerDesiredCount: number;
  readonly unitTestMode?: boolean;
  readonly accessHostInstanceType?: string;
  readonly tavilySecret?: secretsmanager.ISecret;
  readonly anthropicSecret?: secretsmanager.ISecret;
  readonly openaiSecret?: secretsmanager.ISecret;
  readonly schemaBootstrapReady?: Construct;
}

export interface ComputeResources {
  readonly cluster: ecs.Cluster;
  readonly loadBalancer: elbv2.ApplicationLoadBalancer;
}

const PROJECT_ROOT = path.resolve(__dirname, '../../..');
const API_PORT = 8080;
const CONSOLE_PORT = 80;
const DEFAULT_ACCESS_HOST_TYPE = 't3.micro';

const EXECUTION_POLICY = iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AmazonECSTaskExecutionRolePolicy');
const SSM_POLICY = iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSSMManagedInstanceCore');

interface ParsedInstanceType {
  readonly instanceType: ec2.InstanceType;
  readonly cpuType: ec2.AmazonLinuxCpuType;
}

function parseInstanceType(instanceType: string): ParsedInstanceType {
  const [familyToken, sizeToken] = instanceType.toLowerCase().split('.');
  const families: Record<string, ec2.InstanceClass> = {
    t2: ec2.InstanceClass.T2,
    t3: ec2.InstanceClass.T3,
    t3a: ec2.InstanceClass.T3A,
    t4g: ec2.InstanceClass.T4G,
    m5: ec2.InstanceClass.M5,
    m6i: ec2.InstanceClass.M6I,
    m7g: ec2.InstanceClass.M7G,
    c5: ec2.InstanceClass.C5,
    c6i: ec2.InstanceClass.C6I,
    c7g: ec2.InstanceClass.C7G,
  };
  const sizes: Record<string, ec2.InstanceSize> = {
    nano: ec2.InstanceSize.NANO,
    micro: ec2.InstanceSize.MICRO,
    small: ec2.InstanceSize.SMALL,
    medium: ec2.InstanceSize.MEDIUM,
    large: ec2.InstanceSize.LARGE,
    xlarge: ec2.InstanceSize.XLARGE,
    '2xlarge': ec2.InstanceSize.XLARGE2,
    '4xlarge': ec2.InstanceSize.XLARGE4,
    '8xlarge': ec2.InstanceSize.XLARGE8,
    '12xlarge': ec2.InstanceSize.XLARGE12,
    '16xlarge': ec2.InstanceSize.XLARGE16,
    '24xlarge': ec2.InstanceSize.XLARGE24,
  };

  const family = families[familyToken];
  const size = sizes[sizeToken];
  if (!family || !size) {
    return {
      instanceType: ec2.InstanceType.of(ec2.InstanceClass.T3, ec2.InstanceSize.MICRO),
      cpuType: ec2.AmazonLinuxCpuType.X86_64,
    };
  }

  return {
    instanceType: ec2.InstanceType.of(family, size),
    cpuType: familyToken.endsWith('g') ? ec2.AmazonLinuxCpuType.ARM_64 : ec2.AmazonLinuxCpuType.X86_64,
  };
}

function logGroup(scope: Construct, id: string): logs.LogGroup {
  return new logs.LogGroup(scope, id, {
    retention: logs.RetentionDays.ONE_MONTH,
    removalPolicy: RemovalPolicy.DESTROY,
  });
}

function ecsRole(scope: Construct, id: string, managedPolicies: iam.IManagedPolicy[] = []): iam.Role {
  return new iam.Role(scope, id, {
    assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
    managedPolicies,
  });
}

export class ComputeStack extends Stack {
  public readonly cluster: ecs.Cluster;
  public readonly loadBalancer: elbv2.ApplicationLoadBalancer;
  public readonly apiService: ecs.FargateService;
  public readonly consoleService: ecs.FargateService;
  public readonly workerService: ecs.FargateService;
  public readonly modelDiscoveryFunction: lambda.Function;

  constructor(scope: Construct, id: string, props: ComputeStackProps) {
    super(scope, id, props);

    const workerDesiredCount = props.workerDesiredCount;
    const accessHostInstanceType = props.accessHostInstanceType ?? DEFAULT_ACCESS_HOST_TYPE;
    const unitTestMode = props.unitTestMode ?? false;
    const parsedAccessHostInstance = parseInstanceType(accessHostInstanceType);

    const { accessHost: accessHostSg, alb, api, console: consoleSg, worker, lambda: lambdaSg } = props.securityGroups;
    const { database } = props;

    this.cluster = new ecs.Cluster(this, 'Cluster', {
      clusterName: `par-${props.envName}`,
      vpc: props.vpc,
      containerInsights: true,
    });

    const albSubnetSelection = { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS };
    this.loadBalancer = new elbv2.ApplicationLoadBalancer(this, 'InternalLoadBalancer', {
      vpc: props.vpc,
      internetFacing: false,
      securityGroup: alb,
      vpcSubnets: albSubnetSelection,
      loadBalancerName: `par-${props.envName}-alb`,
    });

    const apiTargetGroup = new elbv2.ApplicationTargetGroup(this, 'ApiTargetGroup', {
      vpc: props.vpc,
      port: API_PORT,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
      healthCheck: {
        healthyHttpCodes: '200',
        path: '/v1/health',
        port: `${API_PORT}`,
      },
      targetGroupName: `par-${props.envName}-api`,
    });

    const consoleTargetGroup = new elbv2.ApplicationTargetGroup(this, 'ConsoleTargetGroup', {
      vpc: props.vpc,
      port: CONSOLE_PORT,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
      healthCheck: {
        healthyHttpCodes: '200',
        path: '/healthz',
        port: `${CONSOLE_PORT}`,
      },
      targetGroupName: `par-${props.envName}-console`,
    });

    const listener = this.loadBalancer.addListener('HttpListener', {
      port: 80,
      open: false,
      defaultAction: elbv2.ListenerAction.forward([consoleTargetGroup]),
    });

    listener.addAction('ApiRouting', {
      priority: 10,
      conditions: [elbv2.ListenerCondition.pathPatterns(['/v1/*'])],
      action: elbv2.ListenerAction.forward([apiTargetGroup]),
    });

    const apiImage = unitTestMode
      ? ecs.ContainerImage.fromRegistry('public.ecr.aws/docker/library/nginx:1.27-alpine')
      : ecs.ContainerImage.fromDockerImageAsset(new ecrAssets.DockerImageAsset(this, 'ApiImageAsset', {
          directory: path.join(PROJECT_ROOT, 'services/api-service'),
          platform: ecrAssets.Platform.LINUX_AMD64,
        }));
    const consoleImage = unitTestMode
      ? ecs.ContainerImage.fromRegistry('public.ecr.aws/docker/library/nginx:1.27-alpine')
      : ecs.ContainerImage.fromDockerImageAsset(new ecrAssets.DockerImageAsset(this, 'ConsoleImageAsset', {
          directory: path.join(PROJECT_ROOT, 'services/console'),
          platform: ecrAssets.Platform.LINUX_AMD64,
        }));
    const workerImage = unitTestMode
      ? ecs.ContainerImage.fromRegistry('public.ecr.aws/docker/library/python:3.11-slim')
      : ecs.ContainerImage.fromDockerImageAsset(new ecrAssets.DockerImageAsset(this, 'WorkerImageAsset', {
          directory: path.join(PROJECT_ROOT, 'services/worker-service'),
          platform: ecrAssets.Platform.LINUX_AMD64,
        }));
    const apiTaskRole = ecsRole(this, 'ApiTaskRole');
    const apiTaskDefinition = new ecs.FargateTaskDefinition(this, 'ApiTaskDefinition', {
      cpu: 512,
      memoryLimitMiB: 1024,
      taskRole: apiTaskRole,
    });
    const apiExecutionRole = apiTaskDefinition.obtainExecutionRole();
    apiExecutionRole.addManagedPolicy(EXECUTION_POLICY);
    const apiLogGroup = logGroup(this, 'ApiLogGroup');
    const apiContainer = apiTaskDefinition.addContainer('ApiContainer', {
      image: apiImage,
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'api',
        logGroup: apiLogGroup,
      }),
      environment: {
        DB_HOST: database.host,
        DB_PORT: database.port.toString(),
        DB_NAME: database.name,
        SERVER_PORT: API_PORT.toString(),
        ...(unitTestMode ? { DB_USER: 'test-user', DB_PASSWORD: 'test-password' } : {}),
      },
      ...(unitTestMode
        ? {}
        : {
            secrets: {
              DB_USER: ecs.Secret.fromSecretsManager(database.credentialsSecret, 'username'),
              DB_PASSWORD: ecs.Secret.fromSecretsManager(database.credentialsSecret, 'password'),
            },
          }),
    });
    apiContainer.addPortMappings({ containerPort: API_PORT, protocol: ecs.Protocol.TCP });

    this.apiService = new ecs.FargateService(this, 'ApiService', {
      cluster: this.cluster,
      taskDefinition: apiTaskDefinition,
      desiredCount: 1,
      assignPublicIp: false,
      securityGroups: [api],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      healthCheckGracePeriod: Duration.seconds(120),
    });
    this.apiService.attachToApplicationTargetGroup(apiTargetGroup);
    this.apiService.autoScaleTaskCount({
      minCapacity: 1,
      maxCapacity: 4,
    }).scaleOnCpuUtilization('ApiCpuScaling', {
      targetUtilizationPercent: 60,
    });

    const consoleTaskRole = ecsRole(this, 'ConsoleTaskRole');
    const consoleTaskDefinition = new ecs.FargateTaskDefinition(this, 'ConsoleTaskDefinition', {
      cpu: 256,
      memoryLimitMiB: 512,
      taskRole: consoleTaskRole,
    });
    const consoleExecutionRole = consoleTaskDefinition.obtainExecutionRole();
    consoleExecutionRole.addManagedPolicy(EXECUTION_POLICY);
    const consoleLogGroup = logGroup(this, 'ConsoleLogGroup');
    const consoleContainer = consoleTaskDefinition.addContainer('ConsoleContainer', {
      image: consoleImage,
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'console',
        logGroup: consoleLogGroup,
      }),
    });
    consoleContainer.addPortMappings({ containerPort: CONSOLE_PORT, protocol: ecs.Protocol.TCP });

    this.consoleService = new ecs.FargateService(this, 'ConsoleService', {
      cluster: this.cluster,
      taskDefinition: consoleTaskDefinition,
      desiredCount: 1,
      assignPublicIp: false,
      securityGroups: [consoleSg],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
    });
    this.consoleService.attachToApplicationTargetGroup(consoleTargetGroup);

    const workerTaskRole = ecsRole(this, 'WorkerTaskRole');
    const workerTaskDefinition = new ecs.FargateTaskDefinition(this, 'WorkerTaskDefinition', {
      cpu: 1024,
      memoryLimitMiB: 2048,
      taskRole: workerTaskRole,
    });
    const workerExecutionRole = workerTaskDefinition.obtainExecutionRole();
    workerExecutionRole.addManagedPolicy(EXECUTION_POLICY);
    const workerLogGroup = logGroup(this, 'WorkerLogGroup');
    workerTaskDefinition.addContainer('WorkerContainer', {
      image: workerImage,
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'worker',
        logGroup: workerLogGroup,
      }),
      environment: {
        DB_HOST: database.host,
        DB_PORT: database.port.toString(),
        DB_NAME: database.name,
        ...(unitTestMode ? { DB_USER: 'test-user', DB_PASSWORD: 'test-password' } : {}),
      },
      ...(unitTestMode
        ? {}
        : {
            secrets: {
              DB_USER: ecs.Secret.fromSecretsManager(database.credentialsSecret, 'username'),
              DB_PASSWORD: ecs.Secret.fromSecretsManager(database.credentialsSecret, 'password'),
              ...(props.tavilySecret ? { TAVILY_API_KEY: ecs.Secret.fromSecretsManager(props.tavilySecret) } : {}),
            },
          }),
    });

    this.workerService = new ecs.FargateService(this, 'WorkerService', {
      cluster: this.cluster,
      taskDefinition: workerTaskDefinition,
      desiredCount: workerDesiredCount,
      assignPublicIp: false,
      securityGroups: [worker],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
    });

    if (unitTestMode) {
      this.modelDiscoveryFunction = new lambda.Function(this, 'ModelDiscoveryFunction', {
        functionName: `par-${props.envName}-model-discovery`,
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: 'index.handler',
        code: lambda.Code.fromInline('def handler(event, context):\n    return {"statusCode": 200}\n'),
        memorySize: 256,
        timeout: Duration.seconds(120),
        vpc: props.vpc,
        vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
        securityGroups: [lambdaSg],
        environment: {
          DB_HOST: database.host,
          DB_PORT: database.port.toString(),
          DB_NAME: database.name,
          DB_CREDENTIALS_SECRET_ARN: database.credentialsSecret.secretArn,
          ...(props.anthropicSecret ? { ANTHROPIC_API_KEY_SECRET_ARN: props.anthropicSecret.secretArn } : {}),
          ...(props.openaiSecret ? { OPENAI_API_KEY_SECRET_ARN: props.openaiSecret.secretArn } : {}),
        },
      });
    } else {
      this.modelDiscoveryFunction = new lambda.DockerImageFunction(this, 'ModelDiscoveryFunction', {
        functionName: `par-${props.envName}-model-discovery`,
        code: lambda.DockerImageCode.fromImageAsset(path.join(PROJECT_ROOT, 'services/model-discovery'), {
          platform: ecrAssets.Platform.LINUX_AMD64,
        }),
        memorySize: 256,
        timeout: Duration.seconds(120),
        architecture: lambda.Architecture.X86_64,
        vpc: props.vpc,
        vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
        securityGroups: [lambdaSg],
        environment: {
          DB_HOST: database.host,
          DB_PORT: database.port.toString(),
          DB_NAME: database.name,
          DB_CREDENTIALS_SECRET_ARN: database.credentialsSecret.secretArn,
          ...(props.anthropicSecret ? { ANTHROPIC_API_KEY_SECRET_ARN: props.anthropicSecret.secretArn } : {}),
          ...(props.openaiSecret ? { OPENAI_API_KEY_SECRET_ARN: props.openaiSecret.secretArn } : {}),
        },
      });
    }
    if (!unitTestMode) {
      database.credentialsSecret.grantRead(this.modelDiscoveryFunction);
      props.anthropicSecret?.grantRead(this.modelDiscoveryFunction);
      props.openaiSecret?.grantRead(this.modelDiscoveryFunction);
    }

    const discoverySchedule = new events.Rule(this, 'ModelDiscoverySchedule', {
      schedule: events.Schedule.rate(Duration.days(1)),
    });
    discoverySchedule.addTarget(new eventsTargets.LambdaFunction(this.modelDiscoveryFunction));

    const initialDiscoveryHandler = new lambda.Function(this, 'InitialModelDiscoveryHandler', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      timeout: Duration.seconds(120),
      memorySize: 256,
      logRetention: logs.RetentionDays.ONE_MONTH,
      code: lambda.Code.fromInline(`
import boto3

lambda_client = boto3.client("lambda")

def handler(event, context):
    physical_resource_id = event.get("PhysicalResourceId", "initial-model-discovery")
    if event["RequestType"] == "Delete":
        return {"PhysicalResourceId": physical_resource_id}

    function_name = event["ResourceProperties"]["FunctionName"]
    response = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
    )

    payload_bytes = response["Payload"].read()
    payload_text = payload_bytes.decode("utf-8") if payload_bytes else ""

    if response.get("FunctionError"):
        raise RuntimeError(
            f"Model discovery invoke failed with {response['FunctionError']}: {payload_text}"
        )

    return {
        "PhysicalResourceId": physical_resource_id,
        "Data": {"Payload": payload_text},
    }
`),
    });
    this.modelDiscoveryFunction.grantInvoke(initialDiscoveryHandler);

    const initialDiscoveryProvider = new cr.Provider(this, 'InitialModelDiscoveryProvider', {
      onEventHandler: initialDiscoveryHandler,
    });

    const initialDiscoveryInvoke = new CustomResource(this, 'InitialModelDiscoveryInvoke', {
      serviceToken: initialDiscoveryProvider.serviceToken,
      properties: {
        FunctionName: this.modelDiscoveryFunction.functionName,
        FunctionRevision: this.modelDiscoveryFunction.currentVersion.functionArn,
        AnthropicSecretArn: props.anthropicSecret?.secretArn ?? '',
        OpenAiSecretArn: props.openaiSecret?.secretArn ?? '',
      },
    });
    if (props.schemaBootstrapReady) {
      initialDiscoveryInvoke.node.addDependency(props.schemaBootstrapReady);
    }

    const accessHost = new ec2.Instance(this, 'AccessHost', {
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      securityGroup: accessHostSg,
      instanceType: parsedAccessHostInstance.instanceType,
      machineImage: ec2.MachineImage.latestAmazonLinux2023({
        cpuType: parsedAccessHostInstance.cpuType,
      }),
      role: new iam.Role(this, 'AccessHostRole', {
        assumedBy: new iam.ServicePrincipal('ec2.amazonaws.com'),
        managedPolicies: [SSM_POLICY],
      }),
      associatePublicIpAddress: true,
    });
    accessHost.addUserData('systemctl enable amazon-ssm-agent || true', 'systemctl start amazon-ssm-agent || true');

    new CfnOutput(this, 'InternalAlbDnsName', {
      value: this.loadBalancer.loadBalancerDnsName,
      exportName: `par-${props.envName}-internal-alb-dns`,
    });
    new CfnOutput(this, 'AccessHostInstanceId', {
      value: accessHost.instanceId,
      exportName: `par-${props.envName}-access-host-instance-id`,
    });
    new CfnOutput(this, 'ModelDiscoveryFunctionArn', {
      value: this.modelDiscoveryFunction.functionArn,
      exportName: `par-${props.envName}-model-discovery-arn`,
    });
  }
}
