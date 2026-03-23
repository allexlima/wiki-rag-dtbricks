"""
MediaWiki on ECS Fargate with ALB.

Single Fargate task behind an Application Load Balancer.
The ALB provides a stable public DNS for Databricks jobs/apps to reach MediaWiki.

Architecture:
    Internet → ALB (:80) → Fargate task (MediaWiki :80) → Lakebase PostgreSQL
    Secrets:   AWS Secrets Manager (synced from Databricks by deploy.sh)
    Compute:   ARM64 Graviton (cheaper than x86)
    Network:   Public subnets (ALB) + private subnets (Fargate) + 1 NAT gateway
               deploy.sh pre-allocates the NAT Elastic IP and adds it to the
               Databricks workspace IP ACL *before* CDK deploy, so the ECS
               health check can reach Lakebase from the first boot.
"""

from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_ecs_patterns as ecs_patterns
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as sm
from constructs import Construct

# Path to mediawiki/ directory (Dockerfile context)
DOCKER_CONTEXT = str(Path(__file__).resolve().parent.parent)

# Default values for context parameters (overridable via -c flags)
DEFAULTS = {
    "lakebase_host": "",
    "lakebase_port": "5432",
    "lakebase_db": "wikidb",
    "lakebase_user": "mediawiki",
    "mw_admin_user": "Admin",
    "secret_name": "wiki-rag/mediawiki",
    "allowed_cidr": "0.0.0.0/0",
    "nat_eip_alloc_id": "",
}


class MediaWikiStack(Stack):
    """MediaWiki on ECS Fargate backed by Lakebase PostgreSQL.

    Resources created:
        - VPC with public + private subnets (2 AZs, 1 NAT gateway)
        - ECS cluster + Fargate service (1 task, ARM64, private subnet)
        - Application Load Balancer (internet-facing, port 80)
        - CloudWatch log group (7-day retention, auto-deleted on teardown)
        - NAT gateway with Elastic IP (stable outbound IP for Lakebase ACL)
    """

    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(
            scope,
            id,
            description="MediaWiki on ECS Fargate backed by Lakebase PostgreSQL (wiki-rag demo)",
            **kwargs,
        )

        # -- Context parameters (passed via deploy.sh -c flags) --
        ctx = {k: self.node.try_get_context(k) or v for k, v in DEFAULTS.items()}
        nat_eip_alloc_id = ctx["nat_eip_alloc_id"]

        # -- Secrets: synced from Databricks scope to AWS by deploy.sh --
        mw_secret = sm.Secret.from_secret_name_v2(
            self, "MwSecret", ctx["secret_name"],
        )

        # -- Network --
        # When deploy.sh pre-allocates a NAT EIP (the default path),
        # we create the VPC without built-in NAT and wire it manually.
        # This lets deploy.sh add the IP to Databricks IP ACL *before*
        # CDK deploy, so the health check passes on first boot.
        if nat_eip_alloc_id:
            vpc = ec2.Vpc(
                self,
                "Vpc",
                max_azs=2,
                nat_gateways=0,
                subnet_configuration=[
                    ec2.SubnetConfiguration(
                        name="Public",
                        subnet_type=ec2.SubnetType.PUBLIC,
                    ),
                    ec2.SubnetConfiguration(
                        name="Private",
                        subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    ),
                ],
            )

            # NAT gateway using pre-allocated EIP
            nat_gw = ec2.CfnNatGateway(
                self,
                "NatGateway",
                subnet_id=vpc.public_subnets[0].subnet_id,
                allocation_id=nat_eip_alloc_id,
                tags=[{"key": "Name", "value": "wiki-rag-nat"}],
            )

            # Route isolated subnets through NAT
            for i, subnet in enumerate(vpc.isolated_subnets):
                ec2.CfnRoute(
                    self,
                    f"NatRoute{i}",
                    route_table_id=subnet.route_table.route_table_id,
                    destination_cidr_block="0.0.0.0/0",
                    nat_gateway_id=nat_gw.ref,
                )

            private_subnets = ec2.SubnetSelection(
                subnets=vpc.isolated_subnets,
            )
        else:
            # Fallback: let CDK manage NAT (for manual cdk deploy
            # without deploy.sh). EIP won't be pre-registered in
            # Databricks IP ACL — user must add it manually.
            vpc = ec2.Vpc(
                self,
                "Vpc",
                max_azs=2,
                nat_gateways=1,
                subnet_configuration=[
                    ec2.SubnetConfiguration(
                        name="Public",
                        subnet_type=ec2.SubnetType.PUBLIC,
                    ),
                    ec2.SubnetConfiguration(
                        name="Private",
                        subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    ),
                ],
            )
            private_subnets = ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
            )

        # -- ECS cluster --
        cluster = ecs.Cluster(self, "Cluster", vpc=vpc)

        # -- CloudWatch log group (auto-deleted with the stack) --
        log_group = logs.LogGroup(
            self,
            "LogGroup",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # -- Fargate service behind ALB --
        service = ecs_patterns.ApplicationLoadBalancedFargateService(
            self,
            "Service",
            cluster=cluster,
            # Compute: 0.25 vCPU, 1 GB — enough for a demo wiki
            cpu=512,
            memory_limit_mib=1024,
            desired_count=1,
            # Zero-downtime deploy: new task starts before old one stops
            min_healthy_percent=100,
            # Fargate in private subnets — outbound via NAT (stable IP)
            task_subnets=private_subnets,
            assign_public_ip=False,
            # ALB: internet-facing; ingress rule added separately via
            # CfnSecurityGroupIngress (inline rules get silently dropped)
            public_load_balancer=True,
            open_listener=False,
            # Grace period: MediaWiki bootstrap (install + update.php) can
            # take up to ~90s on first boot; prevent premature task kills
            health_check_grace_period=Duration.seconds(180),
            # ARM64 Graviton: ~20% cheaper than x86
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
            task_image_options=ecs_patterns.ApplicationLoadBalancedTaskImageOptions(
                # Docker image built from mediawiki/Dockerfile
                image=ecs.ContainerImage.from_asset(
                    DOCKER_CONTEXT,
                    platform=ecr_assets.Platform.LINUX_ARM64,
                ),
                container_port=80,
                # Plain-text config (non-sensitive)
                environment={
                    "MW_DB_TYPE": "postgres",
                    "LAKEBASE_HOST": ctx["lakebase_host"],
                    "LAKEBASE_PORT": ctx["lakebase_port"],
                    "LAKEBASE_DB": ctx["lakebase_db"],
                    "LAKEBASE_USER": ctx["lakebase_user"],
                    "MW_ADMIN_USER": ctx["mw_admin_user"],
                },
                # Sensitive values injected from Secrets Manager at runtime
                secrets={
                    "LAKEBASE_PASSWORD": ecs.Secret.from_secrets_manager(mw_secret, "mw_password"),
                    "MW_ADMIN_PASSWORD": ecs.Secret.from_secrets_manager(mw_secret, "mw_password"),
                    "MW_SECRET_KEY": ecs.Secret.from_secrets_manager(mw_secret, "mw_secret_key"),
                    "MW_UPGRADE_KEY": ecs.Secret.from_secrets_manager(mw_secret, "mw_upgrade_key"),
                },
                log_driver=ecs.LogDrivers.aws_logs(
                    stream_prefix="mediawiki",
                    log_group=log_group,
                ),
            ),
        )

        # -- ALB security group: allow inbound HTTP.
        #    By default allows all IPs (0.0.0.0/0). Pass
        #    -c allowed_cidr="<ip>/32" to restrict to a single IP.
        #    deploy.sh auto-detects the deployer's public IP.
        #
        #    Uses CfnSecurityGroupIngress (not open_listener or
        #    add_ingress_rule) because inline rules get silently
        #    dropped when the SG also has standalone Egress resources.
        ec2.CfnSecurityGroupIngress(
            self,
            "AlbPublicIngress",
            group_id=(
                service.load_balancer
                .connections.security_groups[0]
                .security_group_id
            ),
            ip_protocol="tcp",
            from_port=80,
            to_port=80,
            cidr_ip=ctx["allowed_cidr"],
            description="HTTP access",
        )

        # -- MW_SERVER_URL: MediaWiki needs to know its own public URL
        #    for generating correct links. Uses the ALB DNS (CloudFormation
        #    token resolved at deploy time). --
        service.task_definition.default_container.add_environment(
            "MW_SERVER_URL",
            f"http://{service.load_balancer.load_balancer_dns_name}",
        )

        # -- Health check: tolerant config for MediaWiki bootstrap.
        #    Accepts 301/302 (MediaWiki redirects short URLs to /wiki/).
        #    5 consecutive failures × 30s = 2.5 min before marking unhealthy. --
        service.target_group.configure_health_check(
            path="/index.php/Main_Page",
            healthy_http_codes="200,301,302",
            interval=Duration.seconds(30),
            timeout=Duration.seconds(15),
            healthy_threshold_count=2,
            unhealthy_threshold_count=5,
        )

        # -- Tags: propagate to all resources in the stack --
        tags = {
            "Environment": "demo",
            "Project": "wiki-rag",
            "Owner": "allex.lima@databricks.com",
            "Repository": "https://github.com/allexlima/wiki-rag-dtbricks",
        }
        for key, value in tags.items():
            Tags.of(self).add(key, value)

        # -- Output: stable ALB DNS for MEDIAWIKI_URL --
        CfnOutput(
            self,
            "MediaWikiUrl",
            value=f"http://{service.load_balancer.load_balancer_dns_name}",
            description="MediaWiki public URL — export as MEDIAWIKI_URL",
        )

        # -- Output: NAT Elastic IP --
        if nat_eip_alloc_id:
            # Pre-allocated by deploy.sh — output the alloc ID
            # (deploy.sh already knows the IP and added it to IP ACL)
            CfnOutput(
                self,
                "NatEipAllocId",
                value=nat_eip_alloc_id,
                description="Pre-allocated NAT EIP allocation ID",
            )
        else:
            # Auto-created by CDK — output the IP for manual ACL setup
            nat_eip = vpc.public_subnets[0].node.find_child("EIP")
            CfnOutput(
                self,
                "NatElasticIp",
                value=nat_eip.ref,
                description="NAT Elastic IP — add to Databricks workspace IP ACL",
            )
