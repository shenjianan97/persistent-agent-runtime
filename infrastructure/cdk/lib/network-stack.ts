import {
  CfnOutput,
  Stack,
  StackProps,
  aws_ec2 as ec2,
} from 'aws-cdk-lib';
import { Construct } from 'constructs';

export interface NetworkStackProps extends StackProps {
  envName: string;
}

export class NetworkStack extends Stack {
  public readonly vpc: ec2.Vpc;
  public readonly publicSubnets: ec2.ISubnet[];
  public readonly privateWithEgressSubnets: ec2.ISubnet[];
  public readonly isolatedSubnets: ec2.ISubnet[];
  public readonly accessHostSecurityGroup: ec2.SecurityGroup;
  public readonly albSecurityGroup: ec2.SecurityGroup;
  public readonly apiSecurityGroup: ec2.SecurityGroup;
  public readonly consoleSecurityGroup: ec2.SecurityGroup;
  public readonly workerSecurityGroup: ec2.SecurityGroup;
  public readonly dbSecurityGroup: ec2.SecurityGroup;
  public readonly lambdaSecurityGroup: ec2.SecurityGroup;

  constructor(scope: Construct, id: string, props: NetworkStackProps) {
    super(scope, id, props);

    this.vpc = new ec2.Vpc(this, 'Vpc', {
      vpcName: `par-${props.envName}`,
      ipAddresses: ec2.IpAddresses.cidr('10.0.0.0/16'),
      maxAzs: 2,
      natGateways: 1,
      subnetConfiguration: [
        {
          name: 'public',
          subnetType: ec2.SubnetType.PUBLIC,
          cidrMask: 24,
        },
        {
          name: 'private-egress',
          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
          cidrMask: 24,
        },
        {
          name: 'isolated',
          subnetType: ec2.SubnetType.PRIVATE_ISOLATED,
          cidrMask: 24,
        },
      ],
    });

    this.publicSubnets = this.vpc.selectSubnets({ subnetType: ec2.SubnetType.PUBLIC }).subnets;
    this.privateWithEgressSubnets = this.vpc.selectSubnets({ subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS }).subnets;
    this.isolatedSubnets = this.vpc.selectSubnets({ subnetType: ec2.SubnetType.PRIVATE_ISOLATED }).subnets;

    this.accessHostSecurityGroup = new ec2.SecurityGroup(this, 'AccessHostSecurityGroup', {
      vpc: this.vpc,
      description: 'SSM-managed access host security group',
      allowAllOutbound: true,
    });

    this.albSecurityGroup = new ec2.SecurityGroup(this, 'AlbSecurityGroup', {
      vpc: this.vpc,
      description: 'Internal ALB security group',
      allowAllOutbound: true,
    });
    this.albSecurityGroup.addIngressRule(this.accessHostSecurityGroup, ec2.Port.tcp(80), 'Access host to internal ALB');

    this.apiSecurityGroup = new ec2.SecurityGroup(this, 'ApiSecurityGroup', {
      vpc: this.vpc,
      description: 'API service security group',
      allowAllOutbound: true,
    });
    this.apiSecurityGroup.addIngressRule(this.albSecurityGroup, ec2.Port.tcp(8080), 'ALB to API');

    this.consoleSecurityGroup = new ec2.SecurityGroup(this, 'ConsoleSecurityGroup', {
      vpc: this.vpc,
      description: 'Console service security group',
      allowAllOutbound: true,
    });
    this.consoleSecurityGroup.addIngressRule(this.albSecurityGroup, ec2.Port.tcp(80), 'ALB to console');

    this.workerSecurityGroup = new ec2.SecurityGroup(this, 'WorkerSecurityGroup', {
      vpc: this.vpc,
      description: 'Worker service security group',
      allowAllOutbound: true,
    });

    this.lambdaSecurityGroup = new ec2.SecurityGroup(this, 'LambdaSecurityGroup', {
      vpc: this.vpc,
      description: 'VPC Lambda security group',
      allowAllOutbound: true,
    });

    this.dbSecurityGroup = new ec2.SecurityGroup(this, 'DatabaseSecurityGroup', {
      vpc: this.vpc,
      description: 'Aurora security group',
      allowAllOutbound: false,
    });
    this.dbSecurityGroup.addIngressRule(this.apiSecurityGroup, ec2.Port.tcp(5432), 'API to DB');
    this.dbSecurityGroup.addIngressRule(this.workerSecurityGroup, ec2.Port.tcp(5432), 'Worker to DB');
    this.dbSecurityGroup.addIngressRule(this.lambdaSecurityGroup, ec2.Port.tcp(5432), 'Lambda to DB');

    new CfnOutput(this, 'VpcId', { value: this.vpc.vpcId });
    new CfnOutput(this, 'PublicSubnetIds', {
      value: this.publicSubnets.map((subnet) => subnet.subnetId).join(','),
    });
    new CfnOutput(this, 'PrivateWithEgressSubnetIds', {
      value: this.privateWithEgressSubnets.map((subnet) => subnet.subnetId).join(','),
    });
    new CfnOutput(this, 'IsolatedSubnetIds', {
      value: this.isolatedSubnets.map((subnet) => subnet.subnetId).join(','),
    });
    new CfnOutput(this, 'AccessHostSecurityGroupId', { value: this.accessHostSecurityGroup.securityGroupId });
    new CfnOutput(this, 'AlbSecurityGroupId', { value: this.albSecurityGroup.securityGroupId });
    new CfnOutput(this, 'ApiSecurityGroupId', { value: this.apiSecurityGroup.securityGroupId });
    new CfnOutput(this, 'ConsoleSecurityGroupId', { value: this.consoleSecurityGroup.securityGroupId });
    new CfnOutput(this, 'WorkerSecurityGroupId', { value: this.workerSecurityGroup.securityGroupId });
    new CfnOutput(this, 'DatabaseSecurityGroupId', { value: this.dbSecurityGroup.securityGroupId });
    new CfnOutput(this, 'LambdaSecurityGroupId', { value: this.lambdaSecurityGroup.securityGroupId });
  }
}
