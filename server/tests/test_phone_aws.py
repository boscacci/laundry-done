"""Validate DynamoDB wire requests, not just the in-memory policy store."""

import sys
from pathlib import Path
import boto3
from botocore.stub import Stubber

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services/phone_alerts"))
from handler import DynamoStore


def test_pending_scan_aliases_reserved_project_name_and_paginates():
    resource = boto3.Session(
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        region_name="us-west-2",
    ).resource("dynamodb")
    store = object.__new__(DynamoStore)
    store.table = resource.Table("jobs")
    args = {
        "TableName": "jobs",
        "FilterExpression": "attribute_exists(#project)",
        "ExpressionAttributeNames": {"#project": "project"},
    }
    with Stubber(store.table.meta.client) as stub:
        stub.add_response(
            "scan",
            {
                "Items": [{"id": {"S": "one"}, "status": {"S": "accepted"}}],
                "LastEvaluatedKey": {"id": {"S": "one"}},
            },
            args,
        )
        stub.add_response(
            "scan",
            {"Items": [{"id": {"S": "two"}, "status": {"S": "completed"}}]},
            {**args, "ExclusiveStartKey": {"id": "one"}},
        )
        assert [row["id"] for row in store.pending()] == ["one"]


def test_standard_outbound_call_does_not_set_campaign_only_ring_timer(monkeypatch):
    import json
    from types import SimpleNamespace
    import handler as cloud
    from dispatcher import MemoryStore

    def dial(**kwargs):
        if "RingTimeoutInSeconds" in kwargs:
            raise ValueError("TrafficType CAMPAIGN is required to enable ring timer")
        return {"ContactId": "contact-1"}

    clients = {
        "connect": SimpleNamespace(start_outbound_voice_contact=dial),
        "s3": SimpleNamespace(head_object=lambda **kw: {}),
        "secretsmanager": SimpleNamespace(
            get_secret_value=lambda **kw: {"SecretString": "+12065550123"}
        ),
    }
    monkeypatch.setattr(cloud.boto3, "client", lambda service: clients[service])
    monkeypatch.setattr(cloud, "DynamoStore", MemoryStore)
    for name, value in {
        "PROJECTS": json.dumps(
            {
                "laundry": {
                    "principal_id": "ROLE1",
                    "daily_limit": 10,
                    "messages": ["test"],
                    "recipient_secret": "secret",
                }
            }
        ),
        "AUDIO_VERSION": "v1",
        "AUDIO_BUCKET": "private",
        "INSTANCE_ID": "instance",
        "FLOW_ID": "flow",
        "SOURCE_NUMBER": "+12065550124",
        "CALLS_ENABLED": "true",
    }.items():
        monkeypatch.setenv(name, value)
    assert (
        cloud.engine().create("laundry", "event", "owner", "test", "ROLE1")["status"]
        == "accepted"
    )
