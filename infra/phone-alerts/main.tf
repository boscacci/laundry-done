terraform {

  required_version = ">= 1.6.0"
  backend "s3" {

    bucket         = "laundry-done-opentofu-state-062008221187"
    key            = "shared-phone-alerts/dev.tfstate"
    region         = "us-west-2"
    dynamodb_table = "iac-dev-box-tf-locks-062008221187-us-west-2"
    encrypt        = true

  }

  required_providers {

    aws = {
      source = "hashicorp/aws", version = "~> 6.0"
    }

    archive = {
      source = "hashicorp/archive", version = "~> 2.7"
    }


  }


}

provider "aws" {

  region = "us-west-2"
  default_tags {
    tags = {
      Project = "shared-phone-alerts", ManagedBy = "opentofu", Environment = "dev"
    }

  }


}

data "aws_caller_identity" "current" {

}

variable "calls_enabled" {
  type    = bool
  default = false
}

variable "certificate_bundle" {
  type        = string
  description = "Public CA certificate only. Never provide a private key."
}

variable "audio_version" {
  type    = string
  default = "v1"
}

locals {
  name = "personal-phone-alerts-dev"
}

resource "aws_connect_instance" "shared" {

  identity_management_type = "CONNECT_MANAGED"
  instance_alias           = "personal-alerts-${data.aws_caller_identity.current.account_id}"
  inbound_calls_enabled    = true
  outbound_calls_enabled   = true
  lifecycle {
    prevent_destroy = true
  }


}

resource "aws_connect_phone_number" "shared" {

  target_arn   = aws_connect_instance.shared.arn
  country_code = "US"
  type         = "DID"
  description  = "Shared personal project alert caller ID"
  lifecycle {
    prevent_destroy = true
  }


}

resource "aws_s3_bucket" "audio" {

  bucket = "personal-phone-audio-${data.aws_caller_identity.current.account_id}"
  lifecycle {
    prevent_destroy = true
  }


}

resource "aws_s3_bucket_public_access_block" "audio" {

  bucket                  = aws_s3_bucket.audio.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true

}

resource "aws_s3_bucket_versioning" "audio" {
  bucket = aws_s3_bucket.audio.id
  versioning_configuration {
    status = "Enabled"
  }

}

resource "aws_s3_bucket_server_side_encryption_configuration" "audio" {

  bucket = aws_s3_bucket.audio.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }

  }


}

resource "aws_s3_bucket_policy" "audio" {

  bucket = aws_s3_bucket.audio.id
  policy = jsonencode({
    Version = "2012-10-17", Statement = [{

      Effect = "Allow", Principal = {
        Service = "connect.amazonaws.com"
      }, Action = "s3:GetObject",
      Resource = "${aws_s3_bucket.audio.arn}/*", Condition = {
        StringEquals = {
          "aws:SourceAccount" = data.aws_caller_identity.current.account_id
          }, ArnEquals = {
          "aws:SourceArn" = aws_connect_instance.shared.arn
        }

      }


      }, {
      Effect = "Deny", Principal = "*", Action = "s3:*", Resource = [aws_s3_bucket.audio.arn, "${aws_s3_bucket.audio.arn}/*"], Condition = {
        Bool = {
          "aws:SecureTransport" = "false"
        }

      }

      }
    ]
    }
  )

}

resource "aws_secretsmanager_secret" "recipient" {
  name = "personal-phone-alerts/owner"
}

resource "aws_dynamodb_table" "jobs" {

  name         = local.name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"
  attribute {
    name = "id"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }


}

resource "aws_iam_role" "lambda" {

  name = local.name
  assume_role_policy = jsonencode({
    Version = "2012-10-17", Statement = [{
      Effect = "Allow", Principal = {
        Service = "lambda.amazonaws.com"
      }, Action = "sts:AssumeRole"
      }
    ]
    }
  )

}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.name}"
  retention_in_days = 30
}

resource "aws_iam_role_policy" "lambda" {

  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17", Statement = [
      {
        Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.lambda.arn}:*"
      },
      {
        Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Scan"], Resource = aws_dynamodb_table.jobs.arn
      },
      {
        Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.recipient.arn
      },
      {
        Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.audio.arn}/laundry/*"
      },
      {
        Effect = "Allow", Action = ["connect:StartOutboundVoiceContact", "connect:StopContact", "connect:DescribeContact", "connect:GetContactAttributes"], Resource = [aws_connect_instance.shared.arn, "${aws_connect_instance.shared.arn}/*"]
      }

    ]
    }
  )

}

resource "aws_rolesanywhere_trust_anchor" "host" {

  name    = local.name
  enabled = true
  source {
    source_type = "CERTIFICATE_BUNDLE"
    source_data {
      x509_certificate_data = var.certificate_bundle
    }

  }


}

resource "aws_iam_role" "laundry" {

  name = "laundry-phone-alerts-dev"
  assume_role_policy = jsonencode({
    Version = "2012-10-17", Statement = [{
      Effect = "Allow", Principal = {
        Service = "rolesanywhere.amazonaws.com"
        }, Action = ["sts:AssumeRole", "sts:TagSession", "sts:SetSourceIdentity"], Condition = {
        ArnEquals = {
          "aws:SourceArn" = aws_rolesanywhere_trust_anchor.host.arn
        }
        StringEquals = { "aws:PrincipalTag/x509Subject/CN" = "laundry-phone-worker-dev" }

      }

      }
    ]
    }
  )

}

resource "aws_rolesanywhere_profile" "host" {

  name             = local.name
  enabled          = true
  role_arns        = [aws_iam_role.laundry.arn]
  duration_seconds = 3600

}

data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = "${path.module}/../../services/phone_alerts"
  output_path = "${path.module}/../../outputs/phone-dispatcher.zip"
  excludes    = ["__pycache__"]
}

resource "aws_lambda_function" "dispatcher" {

  function_name                  = local.name
  role                           = aws_iam_role.lambda.arn
  runtime                        = "python3.13"
  handler                        = "handler.handler"
  filename                       = data.archive_file.lambda.output_path
  source_code_hash               = data.archive_file.lambda.output_base64sha256
  timeout                        = 30
  memory_size                    = 256
  reserved_concurrent_executions = 2
  environment {
    variables = {

      TABLE_NAME    = aws_dynamodb_table.jobs.name
      INSTANCE_ID   = aws_connect_instance.shared.id
      FLOW_ID       = aws_connect_contact_flow.outbound.contact_flow_id
      SOURCE_NUMBER = aws_connect_phone_number.shared.phone_number
      AUDIO_BUCKET  = aws_s3_bucket.audio.id
      AUDIO_VERSION = var.audio_version
      CALLS_ENABLED = tostring(var.calls_enabled)
      PROJECTS = jsonencode({
        laundry = {
          principal_id = aws_iam_role.laundry.unique_id, daily_limit = 10, recipient_secret = aws_secretsmanager_secret.recipient.arn, messages = concat([for n in range(1, 11) : format("laundry-%02d", n)], ["test"])
        }

        }
      )

    }

  }

  depends_on = [aws_cloudwatch_log_group.lambda, aws_iam_role_policy.lambda]

}

# Flow invokes a version-independent ARN, avoiding a Lambda/flow dependency cycle.
locals {

  lambda_arn = "arn:aws:lambda:us-west-2:${data.aws_caller_identity.current.account_id}:function:${local.name}"
  errors = [{
    NextAction = "failed", ErrorType = "NoMatchingError"
    }
  ]

}

resource "aws_connect_lambda_function_association" "callback" {
  instance_id  = aws_connect_instance.shared.id
  function_arn = aws_lambda_function.dispatcher.arn
}

resource "aws_lambda_permission" "connect" {
  statement_id  = "Connect"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.dispatcher.function_name
  principal     = "connect.amazonaws.com"
  source_arn    = aws_connect_instance.shared.arn
}

resource "aws_connect_contact_flow" "outbound" {

  instance_id = aws_connect_instance.shared.id
  name        = "Shared prerecorded alerts"
  type        = "CONTACT_FLOW"
  content = jsonencode({
    Version = "2019-10-30", StartAction = "play", Actions = [
      {
        Identifier = "play", Type = "MessageParticipant", Parameters = {
          Media = {
            Uri = "$.Attributes.audio_uri", SourceType = "S3", MediaType = "Audio"
          }

          }, Transitions = {
          NextAction = "completed", Errors = local.errors
        }

      },
      {
        Identifier = "completed", Type = "InvokeLambdaFunction", Parameters = {
          LambdaFunctionARN = local.lambda_arn, InvocationTimeLimitSeconds = "8", InvocationType = "SYNCHRONOUS", LambdaInvocationAttributes = {
            outcome = "completed"
            }, ResponseValidation = {
            ResponseType = "STRING_MAP"
          }

          }, Transitions = {
          NextAction = "hangup", Errors = [{
            NextAction = "hangup", ErrorType = "NoMatchingError"
            }
          ]
        }

      },
      {
        Identifier = "failed", Type = "InvokeLambdaFunction", Parameters = {
          LambdaFunctionARN = local.lambda_arn, InvocationTimeLimitSeconds = "8", InvocationType = "SYNCHRONOUS", LambdaInvocationAttributes = {
            outcome = "failed"
            }, ResponseValidation = {
            ResponseType = "STRING_MAP"
          }

          }, Transitions = {
          NextAction = "hangup", Errors = [{
            NextAction = "hangup", ErrorType = "NoMatchingError"
            }
          ]
        }

      },
      {
        Identifier = "hangup", Type = "DisconnectParticipant", Parameters = {

          }, Transitions = {

        }

      }

    ]
    }
  )

}

resource "aws_connect_contact_flow" "inbound" {

  instance_id = aws_connect_instance.shared.id
  name        = "Personal project caller identification"
  content = jsonencode({
    Version = "2019-10-30", StartAction = "identify", Actions = [
      {
        Identifier = "identify", Type = "MessageParticipant", Parameters = {
          Text = "This number sends personal project notifications. No action is needed. Goodbye."
          }, Transitions = {
          NextAction = "hangup", Errors = [{
            NextAction = "hangup", ErrorType = "NoMatchingError"
            }
          ]
        }

      },
      {
        Identifier = "hangup", Type = "DisconnectParticipant", Parameters = {

          }, Transitions = {

        }

      }

    ]
    }
  )

}

resource "aws_connect_phone_number_contact_flow_association" "inbound" {
  phone_number_id = aws_connect_phone_number.shared.id
  instance_id     = aws_connect_instance.shared.id
  contact_flow_id = aws_connect_contact_flow.inbound.contact_flow_id
}

resource "aws_api_gateway_rest_api" "calls" {
  name = local.name
  endpoint_configuration {
    types = ["REGIONAL"]
  }

}

resource "aws_api_gateway_resource" "projects" {
  rest_api_id = aws_api_gateway_rest_api.calls.id
  parent_id   = aws_api_gateway_rest_api.calls.root_resource_id
  path_part   = "projects"
}

resource "aws_api_gateway_resource" "project" {
  rest_api_id = aws_api_gateway_rest_api.calls.id
  parent_id   = aws_api_gateway_resource.projects.id
  path_part   = "{project}"
}

resource "aws_api_gateway_resource" "jobs" {
  rest_api_id = aws_api_gateway_rest_api.calls.id
  parent_id   = aws_api_gateway_resource.project.id
  path_part   = "jobs"
}

resource "aws_api_gateway_resource" "event" {
  rest_api_id = aws_api_gateway_rest_api.calls.id
  parent_id   = aws_api_gateway_resource.jobs.id
  path_part   = "{event}"
}

resource "aws_api_gateway_method" "job" {

  for_each      = toset(["PUT", "GET", "DELETE"])
  rest_api_id   = aws_api_gateway_rest_api.calls.id
  resource_id   = aws_api_gateway_resource.event.id
  http_method   = each.key
  authorization = "AWS_IAM"

}

resource "aws_api_gateway_integration" "job" {

  for_each                = aws_api_gateway_method.job
  rest_api_id             = aws_api_gateway_rest_api.calls.id
  resource_id             = aws_api_gateway_resource.event.id
  http_method             = each.key
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.dispatcher.invoke_arn

}

resource "aws_api_gateway_deployment" "calls" {

  rest_api_id = aws_api_gateway_rest_api.calls.id
  triggers = {
    revision = sha256(jsonencode(aws_api_gateway_integration.job))
  }

  lifecycle {
    create_before_destroy = true
  }


}

resource "aws_api_gateway_stage" "dev" {
  rest_api_id   = aws_api_gateway_rest_api.calls.id
  deployment_id = aws_api_gateway_deployment.calls.id
  stage_name    = "dev"
}

resource "aws_lambda_permission" "api" {
  statement_id  = "API"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.dispatcher.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.calls.execution_arn}/*/*/projects/*/jobs/*"
}

resource "aws_iam_role_policy" "laundry" {

  role = aws_iam_role.laundry.id
  policy = jsonencode({
    Version = "2012-10-17", Statement = [{
      Effect = "Allow", Action = "execute-api:Invoke", Resource = [for verb in ["PUT", "GET", "DELETE"] : "${aws_api_gateway_rest_api.calls.execution_arn}/dev/${verb}/projects/laundry/jobs/*"]
      }
    ]
    }
  )

}

resource "aws_cloudwatch_event_rule" "tick" {
  name                = "${local.name}-tick"
  schedule_expression = "rate(1 minute)"
}

resource "aws_cloudwatch_event_rule" "contacts" {
  name = "${local.name}-contacts"
  event_pattern = jsonencode({
    source = ["aws.connect"], "detail-type" = ["Amazon Connect Contact Event"], detail = {
      instanceArn = [aws_connect_instance.shared.arn], eventType = ["DISCONNECTED"]
    }

    }
  )
}

resource "aws_cloudwatch_event_target" "tick" {
  rule = aws_cloudwatch_event_rule.tick.name
  arn  = aws_lambda_function.dispatcher.arn
}

resource "aws_cloudwatch_event_target" "contacts" {
  rule = aws_cloudwatch_event_rule.contacts.name
  arn  = aws_lambda_function.dispatcher.arn
}

resource "aws_lambda_permission" "events" {

  for_each = {
    tick = aws_cloudwatch_event_rule.tick.arn, contacts = aws_cloudwatch_event_rule.contacts.arn
  }

  statement_id  = "Events-${each.key}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.dispatcher.function_name
  principal     = "events.amazonaws.com"
  source_arn    = each.value

}

resource "aws_cloudwatch_metric_alarm" "errors" {

  alarm_name  = "${local.name}-errors"
  namespace   = "AWS/Lambda"
  metric_name = "Errors"
  dimensions = {
    FunctionName = aws_lambda_function.dispatcher.function_name
  }

  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 1
  period              = 60
  statistic           = "Sum"
  treat_missing_data  = "notBreaching"

}

resource "aws_ecr_repository" "relay" {
  name                 = "laundry-done"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }

}

output "api_url" {
  value = aws_api_gateway_stage.dev.invoke_url
}

output "audio_bucket" {
  value = aws_s3_bucket.audio.id
}

output "source_number" {
  value     = aws_connect_phone_number.shared.phone_number
  sensitive = true
}

output "recipient_secret_arn" {
  value = aws_secretsmanager_secret.recipient.arn
}

output "role_arn" {
  value = aws_iam_role.laundry.arn
}

output "trust_anchor_arn" {
  value = aws_rolesanywhere_trust_anchor.host.arn
}

output "profile_arn" {
  value = aws_rolesanywhere_profile.host.arn
}

output "repository_url" {
  value = aws_ecr_repository.relay.repository_url
}

resource "aws_cloudwatch_metric_alarm" "latency" {
  alarm_name          = "${local.name}-slow-dispatcher"
  namespace           = "AWS/Lambda"
  metric_name         = "Duration"
  dimensions          = { FunctionName = aws_lambda_function.dispatcher.function_name }
  comparison_operator = "GreaterThanThreshold"
  threshold           = 25000
  evaluation_periods  = 1
  period              = 60
  statistic           = "Maximum"
  treat_missing_data  = "notBreaching"
}
resource "aws_cloudwatch_metric_alarm" "throttles" {
  alarm_name          = "${local.name}-throttles"
  namespace           = "AWS/Lambda"
  metric_name         = "Throttles"
  dimensions          = { FunctionName = aws_lambda_function.dispatcher.function_name }
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 1
  period              = 60
  statistic           = "Sum"
  treat_missing_data  = "notBreaching"
}
