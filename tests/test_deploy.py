"""Tests for infra/deploy.py.

Only the branching logic is covered -- the parts that decide whether a
provisioning step succeeded, was already done, or failed in a way the operator
must act on. Actual resource creation is exercised by running the script.

deploy.py is loaded from its path because infra/ is not a package.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

DEPLOY_PATH = Path(__file__).resolve().parents[1] / "infra" / "deploy.py"


@pytest.fixture(scope="module")
def deploy():
    spec = importlib.util.spec_from_file_location("carbonshift_deploy", DEPLOY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def client_error(code: str, message: str, operation: str = "CreateServiceLinkedRole"):
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


class FakeIam:
    def __init__(self, raises=None):
        self.calls = []
        self._raises = raises

    def create_service_linked_role(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return {"Role": {"Arn": "arn:aws:iam::111122223333:role/slr"}}


# --- ECS service-linked role ----------------------------------------------


def test_creates_the_service_linked_role_on_a_fresh_account(deploy, capsys):
    iam = FakeIam()

    deploy.ensure_ecs_service_linked_role(iam)

    assert iam.calls == [{"AWSServiceName": "ecs.amazonaws.com"}]
    assert "created" in capsys.readouterr().out


def test_an_existing_role_is_not_an_error(deploy, capsys):
    """IAM reports an already-present service-linked role as InvalidInput."""
    iam = FakeIam(
        raises=client_error(
            "InvalidInput",
            "Service role name AWSServiceRoleForECS has been taken in this account",
        )
    )

    deploy.ensure_ecs_service_linked_role(iam)  # must not raise

    assert "already exists" in capsys.readouterr().out


def test_missing_permission_names_it_and_offers_the_workaround(deploy):
    iam = FakeIam(raises=client_error("AccessDenied", "not authorized"))

    with pytest.raises(deploy.DeployError) as excinfo:
        deploy.ensure_ecs_service_linked_role(iam)

    message = str(excinfo.value)
    assert "iam:CreateServiceLinkedRole" in message
    assert "console" in message


def test_an_unrelated_invalid_input_still_fails(deploy):
    """Only the 'has been taken' case means 'already exists'."""
    iam = FakeIam(raises=client_error("InvalidInput", "something else entirely"))

    with pytest.raises(deploy.DeployError):
        deploy.ensure_ecs_service_linked_role(iam)


# --- Error message quality -------------------------------------------------


def test_explain_names_the_region_and_the_aws_error_code(deploy):
    error = deploy._explain(
        client_error("AccessDenied", "not authorized", "CreateCluster"),
        "Creating the ECS cluster",
    )

    message = str(error)
    assert "Creating the ECS cluster" in message
    assert "AccessDenied" in message
    assert "lacks the IAM permission" in message


def test_explain_passes_through_an_unmapped_code(deploy):
    error = deploy._explain(
        client_error("ThrottlingException", "slow down", "CreateCluster"),
        "Creating the ECS cluster",
    )

    assert "ThrottlingException" in str(error)
    assert "slow down" in str(error)
