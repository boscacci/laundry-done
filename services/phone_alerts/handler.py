"""AWS adapters; the policy engine is independently testable in dispatcher.py."""

import json
import os
import time
from decimal import Decimal

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from dispatcher import Dispatcher, TERMINAL


class DynamoStore:
    def __init__(self):
        self.table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])

    def get(self, key):
        row = self.table.get_item(Key={"id": key}, ConsistentRead=True).get("Item")
        return row

    def put(self, row, expected=None):
        value = dict(
            row, version=int(expected or 0) + 1, ttl=int(row["expires_at"]) + 30 * 86400
        )
        args = {
            "Item": value,
            "ConditionExpression": "attribute_not_exists(id)"
            if expected is None
            else "#v=:v",
        }
        if expected is not None:
            args.update(
                ExpressionAttributeNames={"#v": "version"},
                ExpressionAttributeValues={":v": expected},
            )
        try:
            self.table.put_item(**args)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise RuntimeError("conflict") from exc
            raise
        print(
            json.dumps(
                {
                    "event": "call_state",
                    "job_id": value["id"],
                    "status": value["status"],
                    "attempt": int(value.get("attempt", 0)),
                }
            )
        )
        return value

    def pending(self):
        args = {"FilterExpression": "attribute_exists(project)"}
        while True:
            result = self.table.scan(**args)
            for row in result["Items"]:
                if row["status"] not in TERMINAL:
                    yield row
            if "LastEvaluatedKey" not in result:
                return
            args["ExclusiveStartKey"] = result["LastEvaluatedKey"]

    def reserve(self, project, day, limit, attempt):
        if self.get("attempt:" + attempt):
            return
        serializer = TypeSerializer()

        def item(value):
            return {k: serializer.serialize(v) for k, v in value.items()}

        requests = [
            {
                "Put": {
                    "TableName": self.table.name,
                    "Item": item(
                        {
                            "id": "attempt:" + attempt,
                            "ttl": int(time.time()) + 31 * 86400,
                        }
                    ),
                    "ConditionExpression": "attribute_not_exists(id)",
                }
            }
        ]
        for name, cap in [(project, limit), ("all", 20)]:
            requests.append(
                {
                    "Update": {
                        "TableName": self.table.name,
                        "Key": item({"id": f"quota:{name}:{day}"}),
                        "UpdateExpression": "SET #n=if_not_exists(#n,:zero)+:one, #ttl=:ttl",
                        "ConditionExpression": "attribute_not_exists(#n) OR #n<:cap",
                        "ExpressionAttributeNames": {"#n": "count", "#ttl": "ttl"},
                        "ExpressionAttributeValues": item(
                            {
                                ":zero": 0,
                                ":one": 1,
                                ":cap": cap,
                                ":ttl": (day + 2) * 86400,
                            }
                        ),
                    }
                }
            )
        try:
            boto3.client("dynamodb").transact_write_items(TransactItems=requests)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "TransactionCanceledException":
                if self.get("attempt:" + attempt):
                    return
                raise ValueError("daily quota exceeded") from exc
            raise


def engine():
    connect = boto3.client("connect")
    s3 = boto3.client("s3")
    secrets = boto3.client("secretsmanager")
    projects = json.loads(os.environ["PROJECTS"])

    def dial(job, attempt):
        project = projects[job["project"]]
        destination = secrets.get_secret_value(SecretId=project["recipient_secret"])[
            "SecretString"
        ]
        if (
            not destination.startswith("+1")
            or len(destination) != 12
            or not destination[1:].isdigit()
        ):
            raise ValueError("invalid configured recipient")
        key = f"{job['project']}/{os.environ['AUDIO_VERSION']}/{job['message_key']}.wav"
        try:
            s3.head_object(Bucket=os.environ["AUDIO_BUCKET"], Key=key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in {"404", "NoSuchKey", "NotFound"}:
                raise
            key = f"{job['project']}/{os.environ['AUDIO_VERSION']}/fallback.wav"
            s3.head_object(Bucket=os.environ["AUDIO_BUCKET"], Key=key)
        result = connect.start_outbound_voice_contact(
            InstanceId=os.environ["INSTANCE_ID"],
            ContactFlowId=os.environ["FLOW_ID"],
            DestinationPhoneNumber=destination,
            SourcePhoneNumber=os.environ["SOURCE_NUMBER"],
            ClientToken=f"{job['id']}-{attempt}",
            RingTimeoutInSeconds=45,
            Attributes={
                "job_id": job["id"],
                "project_id": job["project"],
                "audio_uri": f"https://{os.environ['AUDIO_BUCKET']}.s3.us-west-2.amazonaws.com/{key}",
            },
        )
        print(
            json.dumps(
                {"event": "call_accepted", "job_id": job["id"], "attempt": attempt}
            )
        )
        return result["ContactId"]

    def stop(job):
        connect.stop_contact(
            InstanceId=os.environ["INSTANCE_ID"], ContactId=job["contact_id"]
        )

    return Dispatcher(
        DynamoStore(),
        dial,
        projects,
        enabled=os.environ.get("CALLS_ENABLED") == "true",
        stop=stop,
    )


def response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(
            body, default=lambda v: int(v) if isinstance(v, Decimal) else str(v)
        ),
    }


def handler(event, context):
    d = engine()
    try:
        if event.get("source") == "aws.events":
            d.tick()
            return {"ok": True}
        if event.get("source") == "aws.connect":
            detail = event["detail"]
            if (
                detail.get("instanceArn", "").split("/")[-1]
                != os.environ["INSTANCE_ID"]
            ):
                raise ValueError("wrong instance")
            # DISCONNECTED alone cannot prove no-answer. Reconcile the provider
            # record for whether the customer ever connected.
            if detail.get("eventType") == "DISCONNECTED":
                contact = boto3.client("connect").describe_contact(
                    InstanceId=os.environ["INSTANCE_ID"], ContactId=detail["contactId"]
                )["Contact"]
                attrs = boto3.client("connect").get_contact_attributes(
                    InstanceId=os.environ["INSTANCE_ID"],
                    InitialContactId=detail["contactId"],
                )["Attributes"]
                outcome = (
                    "unknown"
                    if contact.get("ConnectedToSystemTimestamp")
                    else "no_answer"
                )
                if attrs.get("job_id"):
                    d.outcome(attrs["job_id"], detail["contactId"], outcome)
            return {"ok": True}
        if "Details" in event:
            data = event["Details"]
            contact = data["ContactData"]
            if contact["InstanceARN"].split("/")[-1] != os.environ["INSTANCE_ID"]:
                raise ValueError("wrong instance")
            attrs = contact["Attributes"]
            kind = data["Parameters"]["outcome"]
            if kind not in {"completed", "failed"}:
                raise ValueError("invalid callback")
            d.outcome(attrs["job_id"], contact["ContactId"], kind)
            return {"ok": "true"}
        path = event.get("pathParameters") or {}
        project = path.get("project")
        event_id = path.get("event")
        identity = event.get("requestContext", {}).get("identity", {})
        principal = identity.get("caller", "").split(":")[0]
        d.authorize(project, principal)
        method = event["httpMethod"]
        if method == "PUT":
            body = json.loads(event.get("body") or "{}")
            if set(body) != {"recipient_id", "message_key", "expires_at"}:
                raise ValueError("invalid fields")
            job = d.create(
                project,
                event_id,
                body["recipient_id"],
                body["message_key"],
                principal,
                int(body["expires_at"]),
            )
        elif method == "GET":
            job = d.store.get(d.identity(project, event_id))
            if not job:
                return response(404, {"error": "job not found"})
        elif method == "DELETE":
            job = d.cancel(project, event_id, principal)
        else:
            return response(405, {"error": "method denied"})
        return response(
            200,
            {
                "job_id": job.get("id"),
                "status": job["status"],
                "attempt": int(job.get("attempt", 0)),
            },
        )
    except ValueError:
        return response(409, {"error": "request denied or calling limit reached"})
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "dispatcher_error",
                    "error": type(exc).__name__,
                    "request_id": context.aws_request_id,
                }
            )
        )
        # Preserve platform retry behavior without logging provider exception
        # strings, which may include the configured phone number.
        raise RuntimeError("dispatcher operation failed") from None
