"""One-time AWS provisioning for CarbonShift.

Creates everything the scheduler depends on, then prints the values to paste
into your ``.env``:

  * an ECR repository to hold the worker image
  * an ECS cluster
  * a CloudWatch log group for the worker
  * an ECS task execution role (pull the image, write logs)
  * an EventBridge Scheduler role allowed to call ecs:RunTask on the worker
  * a Fargate task definition for the worker

Idempotency: every step tolerates the resource already existing and reuses it.
The one exception is the task definition -- ECS has no "update in place" for
those, so re-running registers a new revision. That is expected and harmless;
the new revision ARN is printed and should replace the old one in ``.env``.

    python infra/deploy.py
    python infra/deploy.py --image-uri 1234.dkr.ecr.eu-central-1.amazonaws.com/carbonshift-worker:latest
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError
except ImportError:  # pragma: no cover
    print(
        "ERROR: boto3 is not installed. Run: pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise SystemExit(1)

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is optional
    pass


# --- Configuration ---------------------------------------------------------

AWS_REGION = os.getenv("AWS_REGION", "eu-central-1")

CLUSTER_NAME = "carbonshift-cluster"
ECR_REPOSITORY_NAME = "carbonshift-worker"
TASK_FAMILY = "carbonshift-worker"
CONTAINER_NAME = "carbonshift-worker"  # must match the override in scheduler.py
LOG_GROUP_NAME = "/ecs/carbonshift-worker"
LOG_RETENTION_DAYS = 14

EXECUTION_ROLE_NAME = "carbonshift-ecs-execution-role"
SCHEDULER_ROLE_NAME = "carbonshift-scheduler-role"
SCHEDULER_POLICY_NAME = "carbonshift-scheduler-runtask"

# 2 vCPU / 4 GB, matching the carbon maths in src/scheduler.py.
TASK_CPU = "2048"
TASK_MEMORY = "4096"

# IAM is eventually consistent; give a new role a moment before it is used.
IAM_PROPAGATION_SECONDS = 10


class DeployError(Exception):
    """A provisioning step failed in a way the operator must act on."""


def step(message: str) -> None:
    print(f"==> {message}", flush=True)


def detail(message: str) -> None:
    print(f"    {message}", flush=True)


def _error_code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "")


def _explain(exc: ClientError, what: str) -> DeployError:
    """Turn a botocore error into a message naming the resource or permission."""
    code = _error_code(exc)
    message = exc.response.get("Error", {}).get("Message", str(exc))
    hints = {
        "AccessDenied": "Your AWS identity lacks the IAM permission for this call.",
        "AccessDeniedException": "Your AWS identity lacks the IAM permission for this call.",
        "UnauthorizedOperation": "Your AWS identity lacks the IAM permission for this call.",
        "InvalidClientTokenId": "Your AWS credentials are invalid or expired.",
        "OptInRequired": f"Region {AWS_REGION} is not enabled for this account.",
    }
    hint = hints.get(code, "")
    return DeployError(
        f"{what} failed in {AWS_REGION}. AWS said [{code}]: {message}"
        + (f" -- {hint}" if hint else "")
    )


# --- Steps -----------------------------------------------------------------


def ensure_ecr_repository(ecr: Any) -> str:
    step(f"ECR repository '{ECR_REPOSITORY_NAME}'")
    try:
        response = ecr.create_repository(repositoryName=ECR_REPOSITORY_NAME)
        uri = response["repository"]["repositoryUri"]
        detail("created")
    except ClientError as exc:
        if _error_code(exc) != "RepositoryAlreadyExistsException":
            raise _explain(exc, "Creating the ECR repository")
        response = ecr.describe_repositories(repositoryNames=[ECR_REPOSITORY_NAME])
        uri = response["repositories"][0]["repositoryUri"]
        detail("already exists, reusing")
    detail(uri)
    return uri


def ensure_ecs_service_linked_role(iam: Any) -> None:
    """Create the ECS service-linked role if this account has never used ECS.

    Without it, create_cluster fails with 'Unable to assume the service linked
    role'. AWS creates it automatically when you make a cluster in the console,
    so accounts that have touched ECS before already have it -- but a fresh
    account driven purely by the API does not.
    """
    step("ECS service-linked role")
    try:
        iam.create_service_linked_role(AWSServiceName="ecs.amazonaws.com")
        detail("created")
    except ClientError as exc:
        code = _error_code(exc)
        message = exc.response.get("Error", {}).get("Message", "")
        # IAM reports an existing service-linked role as InvalidInput.
        if code == "InvalidInput" and "has been taken" in message:
            detail("already exists, reusing")
            return
        if code in ("AccessDenied", "AccessDeniedException"):
            raise DeployError(
                "Cannot create the ECS service-linked role: your AWS identity "
                "lacks iam:CreateServiceLinkedRole. Either add that permission, "
                "or create any ECS cluster once in the console (which makes the "
                "role automatically) and re-run this script."
            )
        raise _explain(exc, "Creating the ECS service-linked role")


def ensure_cluster(ecs: Any) -> str:
    step(f"ECS cluster '{CLUSTER_NAME}'")
    try:
        # create_cluster is idempotent: it returns the existing cluster as-is.
        response = ecs.create_cluster(
            clusterName=CLUSTER_NAME, capacityProviders=["FARGATE"]
        )
    except ClientError as exc:
        raise _explain(exc, "Creating the ECS cluster")
    arn = response["cluster"]["clusterArn"]
    detail(arn)
    return arn


def ensure_log_group(logs: Any) -> None:
    step(f"CloudWatch log group '{LOG_GROUP_NAME}'")
    try:
        logs.create_log_group(logGroupName=LOG_GROUP_NAME)
        detail("created")
    except ClientError as exc:
        if _error_code(exc) != "ResourceAlreadyExistsException":
            raise _explain(exc, "Creating the log group")
        detail("already exists, reusing")
    try:
        logs.put_retention_policy(
            logGroupName=LOG_GROUP_NAME, retentionInDays=LOG_RETENTION_DAYS
        )
        detail(f"retention set to {LOG_RETENTION_DAYS} days")
    except ClientError as exc:
        detail(f"could not set retention ({_error_code(exc)}); continuing")


def _ensure_role(iam: Any, name: str, trust_policy: dict, description: str) -> tuple[str, bool]:
    """Create the role if absent. Returns (arn, created_now)."""
    try:
        response = iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description=description,
        )
        detail("created")
        return response["Role"]["Arn"], True
    except ClientError as exc:
        if _error_code(exc) != "EntityAlreadyExists":
            raise _explain(exc, f"Creating IAM role '{name}'")
        response = iam.get_role(RoleName=name)
        detail("already exists, reusing")
        return response["Role"]["Arn"], False


def ensure_execution_role(iam: Any) -> tuple[str, bool]:
    step(f"IAM execution role '{EXECUTION_ROLE_NAME}'")
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    arn, created = _ensure_role(
        iam,
        EXECUTION_ROLE_NAME,
        trust,
        "Lets ECS pull the CarbonShift worker image and write its logs",
    )
    try:
        iam.attach_role_policy(
            RoleName=EXECUTION_ROLE_NAME,
            PolicyArn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
        )
        detail("AmazonECSTaskExecutionRolePolicy attached")
    except ClientError as exc:
        raise _explain(exc, "Attaching the ECS task execution policy")
    detail(arn)
    return arn, created


def ensure_scheduler_role(
    iam: Any, account_id: str, execution_role_arn: str
) -> tuple[str, bool]:
    step(f"IAM scheduler role '{SCHEDULER_ROLE_NAME}'")
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "scheduler.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"aws:SourceAccount": account_id}},
            }
        ],
    }
    arn, created = _ensure_role(
        iam,
        SCHEDULER_ROLE_NAME,
        trust,
        "Lets EventBridge Scheduler run the CarbonShift worker task",
    )

    task_def_arns = f"arn:aws:ecs:{AWS_REGION}:{account_id}:task-definition/{TASK_FAMILY}:*"
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "RunCarbonShiftWorker",
                "Effect": "Allow",
                "Action": "ecs:RunTask",
                "Resource": [task_def_arns],
                "Condition": {
                    "ArnLike": {
                        "ecs:cluster": (
                            f"arn:aws:ecs:{AWS_REGION}:{account_id}:cluster/{CLUSTER_NAME}"
                        )
                    }
                },
            },
            {
                "Sid": "PassTaskRoles",
                "Effect": "Allow",
                "Action": "iam:PassRole",
                "Resource": [execution_role_arn],
                "Condition": {
                    "StringLike": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}
                },
            },
            {
                "Sid": "TagTasks",
                "Effect": "Allow",
                "Action": "ecs:TagResource",
                "Resource": "*",
            },
        ],
    }
    try:
        iam.put_role_policy(
            RoleName=SCHEDULER_ROLE_NAME,
            PolicyName=SCHEDULER_POLICY_NAME,
            PolicyDocument=json.dumps(policy),
        )
        detail(f"inline policy '{SCHEDULER_POLICY_NAME}' written")
    except ClientError as exc:
        raise _explain(exc, "Writing the scheduler inline policy")
    detail(arn)
    return arn, created


def discover_networking(ec2: Any) -> tuple[list[str], list[str]]:
    """Use the account's default VPC rather than creating one."""
    step("Networking (default VPC)")
    try:
        vpcs = ec2.describe_vpcs(
            Filters=[{"Name": "isDefault", "Values": ["true"]}]
        )["Vpcs"]
    except ClientError as exc:
        raise _explain(exc, "Looking up the default VPC")

    if not vpcs:
        raise DeployError(
            f"No default VPC in {AWS_REGION}. Create one (`aws ec2 "
            "create-default-vpc`), or set WORKER_SUBNET_IDS and "
            "WORKER_SECURITY_GROUP_IDS in .env by hand."
        )

    vpc_id = vpcs[0]["VpcId"]
    subnets = [
        subnet["SubnetId"]
        for subnet in ec2.describe_subnets(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        )["Subnets"]
    ]
    if not subnets:
        raise DeployError(f"Default VPC {vpc_id} has no subnets in {AWS_REGION}.")

    groups = [
        group["GroupId"]
        for group in ec2.describe_security_groups(
            Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "group-name", "Values": ["default"]},
            ]
        )["SecurityGroups"]
    ]

    detail(f"vpc {vpc_id}")
    detail(f"subnets {','.join(subnets)}")
    detail(f"security groups {','.join(groups) or '(none found)'}")
    return subnets, groups


def register_task_definition(ecs: Any, image_uri: str, execution_role_arn: str) -> str:
    step(f"ECS task definition '{TASK_FAMILY}'")
    try:
        response = ecs.register_task_definition(
            family=TASK_FAMILY,
            requiresCompatibilities=["FARGATE"],
            networkMode="awsvpc",
            cpu=TASK_CPU,
            memory=TASK_MEMORY,
            executionRoleArn=execution_role_arn,
            containerDefinitions=[
                {
                    "name": CONTAINER_NAME,
                    "image": image_uri,
                    "essential": True,
                    "logConfiguration": {
                        "logDriver": "awslogs",
                        "options": {
                            "awslogs-group": LOG_GROUP_NAME,
                            "awslogs-region": AWS_REGION,
                            "awslogs-stream-prefix": "worker",
                        },
                    },
                }
            ],
        )
    except ClientError as exc:
        raise _explain(exc, "Registering the task definition")

    arn = response["taskDefinition"]["taskDefinitionArn"]
    detail(f"revision {response['taskDefinition']['revision']} registered")
    detail(arn)
    return arn


# --- Entry point -----------------------------------------------------------


def provision(region: str, image_uri_override: str = "") -> tuple[dict[str, str], str]:
    """Create every AWS resource and return the settings it produced.

    Returns (settings, repository_uri). Raises DeployError on failure. Callers
    get the values directly, so nobody has to copy ARNs out of a terminal and
    paste them back into a file by hand.
    """
    global AWS_REGION
    AWS_REGION = region

    session = boto3.session.Session(region_name=AWS_REGION)
    try:
        account_id = session.client("sts").get_caller_identity()["Account"]
    except NoCredentialsError:
        raise DeployError(
            "No AWS credentials found. Run `aws configure` first, or set "
            "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY."
        )
    except ClientError as exc:
        raise _explain(exc, "Identifying your AWS account")

    print(f"CarbonShift provisioning in {AWS_REGION} for account {account_id}")
    print()

    ecr = session.client("ecr")
    ecs = session.client("ecs")
    iam = session.client("iam")
    logs = session.client("logs")
    ec2 = session.client("ec2")

    repository_uri = ensure_ecr_repository(ecr)
    image_uri = image_uri_override or f"{repository_uri}:latest"

    ensure_ecs_service_linked_role(iam)
    cluster_arn = ensure_cluster(ecs)
    ensure_log_group(logs)

    execution_role_arn, exec_created = ensure_execution_role(iam)
    scheduler_role_arn, sched_created = ensure_scheduler_role(
        iam, account_id, execution_role_arn
    )

    if exec_created or sched_created:
        step(f"Waiting {IAM_PROPAGATION_SECONDS}s for IAM to propagate")
        time.sleep(IAM_PROPAGATION_SECONDS)

    subnets, security_groups = discover_networking(ec2)
    task_definition_arn = register_task_definition(ecs, image_uri, execution_role_arn)

    settings = {
        "AWS_REGION": AWS_REGION,
        "ECS_CLUSTER_ARN": cluster_arn,
        "WORKER_TASK_DEFINITION_ARN": task_definition_arn,
        "SCHEDULER_ROLE_ARN": scheduler_role_arn,
        "WORKER_SUBNET_IDS": ",".join(subnets),
        "WORKER_SECURITY_GROUP_IDS": ",".join(security_groups),
        "WORKER_ASSIGN_PUBLIC_IP": "ENABLED",
        "WORKER_IMAGE_URI": image_uri,
    }
    return settings, repository_uri


def push_commands(image_uri: str, region: str) -> list[str]:
    """The four commands that put the worker image where Fargate can pull it."""
    registry = image_uri.split("/")[0]
    return [
        "docker build -t carbonshift-worker src/worker",
        f"aws ecr get-login-password --region {region} | "
        f"docker login --username AWS --password-stdin {registry}",
        f"docker tag carbonshift-worker {image_uri}",
        f"docker push {image_uri}",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="carbonshift-deploy",
        description="Provision the AWS resources CarbonShift needs.",
    )
    parser.add_argument(
        "--image-uri",
        default=os.getenv("WORKER_IMAGE_URI", ""),
        help=(
            "Container image for the worker. Defaults to WORKER_IMAGE_URI, and "
            "then to ':latest' in the ECR repo this script creates."
        ),
    )
    parser.add_argument("--region", default=AWS_REGION, help="AWS region.")
    args = parser.parse_args(argv)

    try:
        settings, _ = provision(args.region, args.image_uri)
    except DeployError as exc:
        print()
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print()
    print("=" * 72)
    print("Provisioning complete. Paste these into your .env:")
    print("=" * 72)
    for key, value in settings.items():
        print(f"{key}={value}")
    print("=" * 72)
    print()
    print("Or skip the pasting entirely by running:  python setup.py")

    if not args.image_uri:
        print()
        print("Next: build and push the worker image before scheduling a run.")
        for command in push_commands(settings["WORKER_IMAGE_URI"],
                                     settings["AWS_REGION"]):
            print(f"  {command}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
