import os
from aws_cdk import (
    Stack,
    CfnOutput,
    Duration,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_ecs_patterns as ecs_patterns,
    aws_secretsmanager as sm,
)
from constructs import Construct


class MediaWikiStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # --- Context parameters ---
        lakebase_host = self.node.try_get_context("lakebase_host") or ""
        lakebase_port = self.node.try_get_context("lakebase_port") or "5432"
        lakebase_db = self.node.try_get_context("lakebase_db") or "wikidb"
        lakebase_user = self.node.try_get_context("lakebase_user") or "mediawiki"
        mw_admin_user = self.node.try_get_context("mw_admin_user") or "Admin"
        secret_name = self.node.try_get_context("secret_name") or "wiki-rag/mediawiki"

        # --- Look up existing AWS Secrets Manager secret ---
        mw_secret = sm.Secret.from_secret_name_v2(
            self, "MwSecret", secret_name
        )

        # --- VPC (simple, public-only — no NAT, lowest cost) ---
        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                )
            ],
        )

        # --- ECS Cluster ---
        cluster = ecs.Cluster(self, "Cluster", vpc=vpc)

        # --- Fargate Service behind ALB ---
        service = ecs_patterns.ApplicationLoadBalancedFargateService(
            self,
            "Service",
            cluster=cluster,
            cpu=512,
            memory_limit_mib=1024,
            desired_count=1,
            assign_public_ip=True,
            task_image_options=ecs_patterns.ApplicationLoadBalancedTaskImageOptions(
                image=ecs.ContainerImage.from_asset(
                    os.path.join(os.path.dirname(__file__), ".."),
                ),
                container_port=80,
                environment={
                    "MW_DB_TYPE": "postgres",
                    "LAKEBASE_HOST": lakebase_host,
                    "LAKEBASE_PORT": lakebase_port,
                    "LAKEBASE_DB": lakebase_db,
                    "LAKEBASE_USER": lakebase_user,
                    "MW_ADMIN_USER": mw_admin_user,
                },
                secrets={
                    "LAKEBASE_PASSWORD": ecs.Secret.from_secrets_manager(
                        mw_secret, "mw_password"
                    ),
                    "MW_ADMIN_PASSWORD": ecs.Secret.from_secrets_manager(
                        mw_secret, "mw_password"
                    ),
                    "MW_SECRET_KEY": ecs.Secret.from_secrets_manager(
                        mw_secret, "mw_secret_key"
                    ),
                    "MW_UPGRADE_KEY": ecs.Secret.from_secrets_manager(
                        mw_secret, "mw_upgrade_key"
                    ),
                },
            ),
        )

        # Set MW_SERVER_URL to ALB DNS (resolved at deploy time via CloudFormation token)
        container = service.task_definition.default_container
        container.add_environment(
            "MW_SERVER_URL",
            f"http://{service.load_balancer.load_balancer_dns_name}",
        )

        # Health check — MediaWiki API siteinfo endpoint
        service.target_group.configure_health_check(
            path="/api.php?action=query&meta=siteinfo&format=json",
            healthy_http_codes="200",
            interval=Duration.seconds(30),
            timeout=Duration.seconds(10),
            healthy_threshold_count=2,
            unhealthy_threshold_count=3,
        )

        # --- Outputs ---
        CfnOutput(
            self,
            "MediaWikiUrl",
            value=f"http://{service.load_balancer.load_balancer_dns_name}",
            description="MediaWiki public URL — use as MEDIAWIKI_URL in databricks.yml",
        )
