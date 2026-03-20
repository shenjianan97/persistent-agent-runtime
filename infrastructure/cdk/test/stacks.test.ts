import { App } from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';

import { DataStack } from '../lib/data-stack';
import { ComputeStack, ComputeSecurityGroups } from '../lib/compute-stack';
import { NetworkStack } from '../lib/network-stack';

function createTemplates() {
  const app = new App({
    context: {
      unitTestMode: true,
    },
  });

  const network = new NetworkStack(app, 'NetworkHarness', {
    envName: 'test',
  });

  const data = new DataStack(app, 'DataHarness', {
    envName: 'test',
    vpc: network.vpc,
    dbSecurityGroup: network.dbSecurityGroup,
    lambdaSecurityGroup: network.lambdaSecurityGroup,
    privateWithEgressSubnets: network.privateWithEgressSubnets,
    isolatedSubnets: network.isolatedSubnets,
    tavilySecretName: 'par/test/tavily',
    anthropicSecretName: 'par/test/anthropic',
    openaiSecretName: 'par/test/openai',
  });

  const securityGroups: ComputeSecurityGroups = {
    accessHost: network.accessHostSecurityGroup,
    alb: network.albSecurityGroup,
    api: network.apiSecurityGroup,
    console: network.consoleSecurityGroup,
    worker: network.workerSecurityGroup,
    db: network.dbSecurityGroup,
    lambda: network.lambdaSecurityGroup,
  };

  const compute = new ComputeStack(app, 'ComputeHarness', {
    envName: 'test',
    vpc: network.vpc,
    securityGroups,
    database: {
      host: data.cluster.clusterEndpoint.hostname,
      port: data.cluster.clusterEndpoint.port,
      name: data.databaseName,
      credentialsSecret: data.databaseCredentialsSecret,
    },
    workerDesiredCount: 1,
    unitTestMode: true,
    accessHostInstanceType: 't3.micro',
    tavilySecret: data.tavilySecret,
    anthropicSecret: data.anthropicSecret,
    openaiSecret: data.openaiSecret,
    schemaBootstrapReady: data.schemaBootstrapResource,
  });

  return {
    network: Template.fromStack(network),
    data: Template.fromStack(data),
    compute: Template.fromStack(compute),
  };
}

describe('Task 8 stacks', () => {
  const templates = createTemplates();

  it('creates the expected network foundation', () => {
    templates.network.hasResourceProperties('AWS::EC2::VPC', {
      EnableDnsHostnames: true,
      EnableDnsSupport: true,
    });
    templates.network.resourceCountIs('AWS::EC2::NatGateway', 1);
    templates.network.resourceCountIs('AWS::EC2::SecurityGroup', 7);
  });

  it('creates the expected data foundation', () => {
    templates.data.hasResourceProperties('AWS::RDS::DBCluster', {
      Engine: 'aurora-postgresql',
      DatabaseName: 'persistent_agent_runtime',
      Port: 5432,
    });
    templates.data.hasResourceProperties('AWS::Lambda::Function', {
      Handler: 'index.handler',
      Runtime: 'nodejs20.x',
    });
    templates.data.resourceCountIs('AWS::CloudFormation::CustomResource', 1);
  });

  it('creates the ECS cluster, ALB, services, and access host', () => {
    templates.compute.hasResourceProperties('AWS::ECS::Cluster', {
      ClusterName: 'par-test',
      ClusterSettings: [
        {
          Name: 'containerInsights',
          Value: 'enabled',
        },
      ],
    });

    templates.compute.hasResourceProperties('AWS::ElasticLoadBalancingV2::LoadBalancer', {
      Scheme: 'internal',
    });

    templates.compute.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', {
      DefaultActions: Match.arrayWith([Match.objectLike({ Type: 'forward' })]),
    });

    templates.compute.hasResourceProperties('AWS::ElasticLoadBalancingV2::ListenerRule', {
      Conditions: Match.arrayWith([
        Match.objectLike({
          Field: 'path-pattern',
          PathPatternConfig: {
            Values: ['/v1/*'],
          },
        }),
      ]),
    });

    templates.compute.resourceCountIs('AWS::ECS::Service', 3);
    templates.compute.resourceCountIs('AWS::ECS::TaskDefinition', 3);
    templates.compute.resourceCountIs('AWS::ElasticLoadBalancingV2::TargetGroup', 2);
    templates.compute.resourceCountIs('AWS::EC2::Instance', 1);
    templates.compute.resourceCountIs('AWS::ApplicationAutoScaling::ScalableTarget', 1);
    templates.compute.resourceCountIs('AWS::ApplicationAutoScaling::ScalingPolicy', 1);
  });

  it('configures the API, worker, console, and model discovery integrations', () => {
    templates.compute.hasResourceProperties('AWS::ECS::Service', {
      HealthCheckGracePeriodSeconds: 120,
    });

    templates.compute.hasResourceProperties('AWS::Lambda::Function', {
      Timeout: 120,
      MemorySize: 256,
    });

    templates.compute.hasResourceProperties('AWS::Events::Rule', {
      ScheduleExpression: 'rate(1 day)',
    });

    templates.compute.resourceCountIs('AWS::CloudFormation::CustomResource', 1);
  });
});
