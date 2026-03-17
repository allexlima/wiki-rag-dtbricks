"""
MediaWiki on ECS Fargate with ALB.

Single Fargate task behind an Application Load Balancer.
The ALB provides a stable public DNS for Databricks jobs/apps to reach MediaWiki.

Architecture:
    Internet → ALB (:80) → Fargate task (MediaWiki :80) → Lakebase PostgreSQL
    Secrets:   AWS Secrets Manager (synced from Databricks by deploy.sh)
    Compute:   ARM64 Graviton (cheaper than x86)
    Network:   Public subnets only, no NAT gateway (lowest cost)
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
}



class MediaWikiStack(Stack):
    """MediaWiki on ECS Fargate backed by Lakebase PostgreSQL.

    Resources created:
        - VPC with public subnets (2 AZs, no NAT)
        - ECS cluster + Fargate service (1 task, ARM64)
        - Application Load Balancer (internet-facing, port 80)
        - CloudWatch log group (7-day retention, auto-deleted on teardown)
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

        # -- Secrets: synced from Databricks scope to AWS by deploy.sh --
        mw_secret = sm.Secret.from_secret_name_v2(
            self, "MwSecret", ctx["secret_name"],
        )

        # -- Network: public subnets only, no NAT (Fargate uses public IP
        #    for ECR image pulls; ALB handles inbound traffic) --
        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,  # ALB requires at least 2 AZs
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                ),
            ],
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
            # Fargate needs public IP to pull images (no NAT gateway)
            assign_public_ip=True,
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

        # -- ALB security group: allow inbound HTTP from the internet.
        #    Both open_listener=True and add_ingress_rule() produce inline
        #    SecurityGroupIngress which CloudFormation silently drops when
        #    the same SG also has standalone Egress resources.  Using a
        #    standalone CfnSecurityGroupIngress resource guarantees the rule
        #    is created as its own CloudFormation resource. --
        ec2.CfnSecurityGroupIngress(
            self,
            "AlbPublicIngress",
            group_id=service.load_balancer.connections.security_groups[0].security_group_id,
            ip_protocol="tcp",
            from_port=80,
            to_port=80,
            cidr_ip="0.0.0.0/0",
            description="HTTP public access",
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
