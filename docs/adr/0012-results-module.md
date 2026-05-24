# ADR-0012: Results Module (Streams Consumer, Analytics Bucket, Glue and Athena)

## Status

Proposed (2026-05-24)

## Context

ADR-0011 settled the *what*: S3 result objects at `extractions/{yyyy}/{mm}/{dd}/{document_id}.json` are the result-delivery sink, with the consumer subscribing to `s3:ObjectCreated:*` on the prefix. This ADR settles the *how*: what mechanism writes those objects, where the bucket lives, and how the same partition is exposed for ad-hoc query.

Three things are bundled into one module because they share one contract—the partition shape:

1. **The publisher.** DynamoDB Streams (enabled in ADR-0007's update) fan the extractor's terminal write out to a small consumer Lambda that writes the result payload to S3.
2. **The analytics bucket.** A separate S3 bucket from the ingestion bucket. Different access pattern (write-once, read-on-demand), different lifecycle, different IAM grants.
3. **The query layer.** A Glue catalog table over the partition, queried through a dedicated Athena workgroup.

Splitting these into three modules would mean three places to update in lockstep every time the partition shape changes. Bundling them keeps the contract co-located with the code that produces and consumes it.

## Decision

A new module at `infra/modules/results/`, wired from `infra/main.tf` next to `bucket`, `queue`, `table`, `extractor`, `uploader`, and `alarms`.

```
infra/modules/results/
  main.tf
  variables.tf
  outputs.tf
  terraform.tf
```

### The publisher

A small zip Lambda—`publisher.py`—subscribed to the results table's DynamoDB Stream via an event source mapping. It does exactly four things per stream record:

1. Skip if the record is not a `MODIFY` or `INSERT` with a terminal `status` (`succeeded` or `failed`). The event source mapping's filter criteria do the primary cut; the in-handler check is defense in depth.
2. Project the `NEW_IMAGE` into the result payload (the DDB item, minus `ttl`).
3. Compose the object key from `created_at` (the day the row was claimed, not the day it was completed—keeps a single document on a single partition even if it crosses midnight between phases).
4. `PutObject` with `ContentType = application/json` and the payload serialized as **single-line JSON** (no pretty-printing). Single-line JSON is what Athena's JSON SerDe expects; a multi-line pretty-printed object would not parse as a row.

```python
{
  "document_id": "...",
  "status": "succeeded",
  "created_at": "2026-05-24T...",
  "completed_at": "2026-05-24T...",
  "extracted_fields": { ... },
  "confidences": { ... },
  "model_version": "...",
  "token_usage": { "input": 1234, "output": 567 },
  "processing_ms": 8912
}
```

`token_usage` riding into the analytics partition is what makes per-day cost attribution one Athena query—without it, cost-by-time-window would need a join against an external bill source.

Event source mapping shape:

```hcl
resource "aws_lambda_event_source_mapping" "publisher" {
  event_source_arn  = var.source_table_stream_arn
  function_name     = aws_lambda_function.publisher.arn
  starting_position = "LATEST"

  batch_size                          = 100
  maximum_batching_window_in_seconds  = 5
  maximum_retry_attempts              = 3
  bisect_batch_on_function_error      = true
  function_response_types             = ["ReportBatchItemFailures"]

  filter_criteria {
    filter {
      pattern = jsonencode({
        eventName = ["INSERT", "MODIFY"]
        dynamodb = {
          NewImage = {
            status = { S = ["succeeded", "failed"] }
          }
        }
      })
    }
  }

  destination_config {
    on_failure {
      destination_arn = aws_sqs_queue.publisher_dlq.arn
    }
  }
}
```

| Lever | Value | Reasoning |
|---|---|---|
| `batch_size = 100` | Up to 100 records per invocation | Stream records are tiny; the consumer is doing one S3 PUT per record. Batching amortizes the Lambda invocation overhead |
| `maximum_batching_window = 5 s` | Wait up to 5 s to fill a batch | Tail-latency on result delivery is dominated by the 5 s window; smaller would invoke more often for no consumer-visible benefit, larger would push delivery latency past the point where the trigger feels event-shaped |
| `bisect_batch_on_function_error = true` | Halve and retry on partial failure | A single poison record in a 100-record batch does not retry the other 99 |
| `function_response_types = ["ReportBatchItemFailures"]` | Per-record failure reporting | Lets the consumer report specific failed records without re-processing the batch |
| `maximum_retry_attempts = 3` | Bound retries before DLQ | Same retry budget as the extractor's `maxReceiveCount = 3` (ADR-0005). Symmetry across the pipeline |
| `on_failure → DLQ` | SQS DLQ owned by this module | Same single-source-of-truth posture as ADR-0005's queue DLQ; the alarms module's existing pattern fits without change |

Idempotency: the consumer is naturally idempotent because the S3 key is `{document_id}.json` and a redelivered stream record writes the same bytes to the same key. `PutObject` overwrites; no `If-None-Match` is needed. The result payload is a pure function of the `NEW_IMAGE`, so identical inputs produce identical objects.

Function sizing—same posture as the uploader's presigner (ADR-0010): zip Lambda (no heavy dependencies—`boto3` and the standard library), 256 MB memory, 30 s timeout (generous for one S3 PUT per record times 100 records), `arm64`.

### The analytics bucket

A second S3 bucket—`${project_name}-${environment}-extractions-{suffix}`—separate from the ingestion bucket. Same four-layer hardening posture as ADR-0003 (Public Access Block, BucketOwnerEnforced, TLS-only policy, AES256 default encryption). Versioning enabled (recovery substrate, same reasoning as ingestion). EventBridge notifications enabled so the caller's subscription can be installed on the bucket's default-bus emissions.

| Setting | Value | Reasoning |
|---|---|---|
| Public Access Block | All four flags | ADR-0003 reasoning applies unchanged |
| Ownership controls | `BucketOwnerEnforced` | Disables ACLs; uploads come from one IAM role (the publisher) |
| TLS-only bucket policy | Deny `aws:SecureTransport = false` | Pipeline-wide transport posture |
| Default encryption | SSE-S3 (AES256) | Parity with ADR-0004; CMK migration moves with the rest of the pipeline at the same boundary |
| Versioning | Enabled | Recovery from accidental overwrite or delete |
| EventBridge notifications | Enabled | The mechanism the caller subscribes to per ADR-0011 |
| Lifecycle (current) | Stays in `STANDARD` | Athena cannot query Glacier-tier objects; transitioning result objects to cold storage would silently break analytics |
| Lifecycle (noncurrent versions) | Expire at 30 days | Bounds versioning storage cost without losing the recovery window |
| Lifecycle (incomplete multipart) | Abort at 7 days | Hygiene; matches ingestion |
| Lifecycle (current expiration) | **None at MVP** | Results are the durable record of work. Adding an expiration would couple "how long results exist" to a retention policy we have not decided. Easy to add later as a tfvar |

The bucket has **no** lifecycle transition to IA or Glacier. The ingestion bucket can transition because the source documents are read once at extraction and rarely after; the analytics bucket cannot, because Athena queries the same objects on-demand and Athena does not transparently restore from Glacier—a query that hits a Glacier object errors out. Keeping results in `STANDARD` is the only tier compatible with the query layer this module also owns.

Caller-side access: the caller's IAM role needs `s3:GetObject` on `${analytics_bucket}/extractions/*`. That grant lives on the caller's side, not in this module. The module outputs the bucket ARN so the caller can scope its grant precisely.

### The query layer

A Glue database, a Glue table over the partition with **partition projection**, and a dedicated Athena workgroup.

**Glue table, not crawler.** The result schema is bounded and project-owned; a crawler would periodically rediscover what we already know, with its own IAM surface, schedule, and cost. The table is defined in Terraform:

```hcl
resource "aws_glue_catalog_database" "results" {
  name = "${var.project_name}_${var.environment}_results"
}

resource "aws_glue_catalog_table" "extractions" {
  database_name = aws_glue_catalog_database.results.name
  name          = "extractions"
  table_type    = "EXTERNAL_TABLE"

  parameters = {
    "classification"                       = "json"
    "projection.enabled"                   = "true"
    "projection.year.type"                 = "integer"
    "projection.year.range"                = "2026,2030"
    "projection.month.type"                = "integer"
    "projection.month.range"               = "1,12"
    "projection.month.digits"              = "2"
    "projection.day.type"                  = "integer"
    "projection.day.range"                 = "1,31"
    "projection.day.digits"                = "2"
    "storage.location.template"            = "s3://${module.results.bucket_name}/extractions/$${year}/$${month}/$${day}/"
  }

  partition_keys {
    name = "year"
    type = "int"
  }
  partition_keys {
    name = "month"
    type = "int"
  }
  partition_keys {
    name = "day"
    type = "int"
  }

  storage_descriptor {
    location      = "s3://${module.results.bucket_name}/extractions/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.openx.data.jsonserde.JsonSerDe"
      parameters = {
        "ignore.malformed.json" = "true"
      }
    }

    # columns: document_id, status, created_at, completed_at, extracted_fields,
    # confidences, model_version, token_usage, processing_ms, error
  }
}
```

**Partition projection, not `MSCK REPAIR`.** Projection computes partitions from a path template at query time, so the catalog does not need to be told about each new day. No periodic `MSCK REPAIR TABLE` job, no Lambda that fires on PUT, no out-of-band script. The day range is bounded above by a value far in the future (`2030`); pushing it out later is a one-line table-parameter change.

**Dedicated Athena workgroup.** A workgroup pinned to its own query-result bucket (`${analytics_bucket}-athena-results`), with CloudWatch metrics enabled and a per-query data-scan limit set. The workgroup is a budget boundary as much as a permissions boundary—Athena bills per TB scanned, and an unbounded workgroup is a single bad query away from a surprise. The default scan limit at MVP is **1 GB per query**; raising it is an opt-in.

```hcl
resource "aws_athena_workgroup" "results" {
  name = "${var.project_name}-${var.environment}-results"

  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true

    result_configuration {
      output_location = "s3://${aws_s3_bucket.athena_results.bucket}/"
      encryption_configuration {
        encryption_option = "SSE_S3"
      }
    }

    bytes_scanned_cutoff_per_query = 1073741824  # 1 GB
  }
}
```

The Athena results bucket is a separate, smaller bucket. Same hardening posture as the analytics bucket. Lifecycle expires query results at **7 days**—query results are debugging artifacts, not durable data.

### Module wiring

```hcl
module "results" {
  source                  = "./modules/results"
  bucket_name             = "${var.project_name}-${var.environment}-extractions-${local.results_bucket_suffix}"
  source_table_arn        = module.table.table_arn
  source_table_stream_arn = module.table.stream_arn
  function_name           = "${var.project_name}-${var.environment}-publisher"
  log_retention_days      = var.environment == "prod" ? 30 : 14
  environment             = var.environment
  alarm_topic_arn         = module.alarms.topic_arn
}
```

ADR-0007's update exposes `stream_arn` as a module output. Without it, this module cannot subscribe.

### IAM execution role (publisher)

Inline policy, two statements:

| Statement | Action | Resource |
|---|---|---|
| `StreamsConsume` | `dynamodb:DescribeStream`, `dynamodb:GetRecords`, `dynamodb:GetShardIterator`, `dynamodb:ListStreams` | `${var.source_table_stream_arn}` |
| `ResultsWrite` | `s3:PutObject` | `${aws_s3_bucket.results.arn}/extractions/*` |
| `LogsWrite` | `logs:CreateLogStream`, `logs:PutLogEvents` | `${aws_cloudwatch_log_group.publisher.arn}:*` |

Scoped to the `extractions/` prefix on PUT so the publisher cannot write to the Athena results bucket or anywhere else. Same `Environment` tag and `DenyTouchingOtherEnvs` posture as the rest of the modules.

### Observability

Same posture as the extractor and uploader: module-owned log group, structured JSON logs with `document_id`, `stream_record_id`, and `handler_outcome` (`published` | `skipped_non_terminal` | `failed`). Retention 14 d in `local`/`staging`, 30 d in `prod`.

Function-level alarms on the same SNS topic as the rest of the pipeline:

| Alarm | Metric | Threshold | Why |
|---|---|---|---|
| `${function_name}-errors` | `AWS/Lambda` `Errors` (Sum) | `> 0` over 5 min | Catches publisher exceptions; without this, results silently stop reaching S3 while the extractor keeps writing to DDB |
| `${publisher_dlq_name}-messages-visible` | `AWS/SQS` `ApproximateNumberOfMessagesVisible` (Max) on the publisher DLQ | `> 0` over 5 min | Single source of truth for failed stream batches; mirrors ADR-0009's extractor DLQ alarm |

`IteratorAge` on the stream event source mapping is the metric that *does* apply here (unlike on SQS-Lambda, where ADR-0009 noted it does not)—it is the lag between the stream record being written and the consumer processing it. Deferred to a future observability ADR alongside end-to-end latency SLOs; the alarm shape (`IteratorAge > N seconds`) is straightforward but the threshold is a "real traffic" decision, not a "design time" one.

## Consequences

Positive:

- The extractor's hot path is unchanged. The DDB → S3 hop is asynchronous, owned by this module, and does not enlarge the extractor's failure surface.
- The result object, the analytics partition, and any future audit read are the same bytes. One source of truth at the consumption boundary.
- `token_usage` in the analytics partition makes per-time-window cost attribution a single Athena query—no join against external billing data.
- Partition projection eliminates the catalog-maintenance class of bugs (`MSCK REPAIR TABLE` forgotten, partitions missing in Athena for objects that exist in S3). Adding a day to the available partition range costs nothing.
- The query layer has a budget boundary by default (the workgroup's scan-cutoff). An accidental `SELECT *` does not become a surprise bill.
- One module, one partition contract. The publisher, the bucket, and the catalog table cannot drift from each other because they live in the same Terraform module.

Negative:

- DynamoDB Streams is now load-bearing for result delivery. A Streams outage or a stuck consumer delays delivery even though extraction itself succeeded. Mitigated by Streams' 24-hour retention and the on-failure DLQ.
- The analytics bucket cannot use cold-tier lifecycle transitions because Athena cannot query Glacier objects. Storage cost grows linearly with results forever unless an expiration policy is added—recorded as a deliberate MVP choice, not an oversight.
- Two Lambdas are added (publisher + future alarm action handlers if any), and one more SQS queue (publisher DLQ). The operational surface grows.
- Glue schema is checked in alongside the result payload shape. Changing the result shape requires updating the Glue table—coupled by design, but a coupling the schema-by-crawler alternative would have hidden until query time.

Neutral:

- The Glue table uses `org.openx.data.jsonserde.JsonSerDe` with `ignore.malformed.json = true`. A malformed object (the publisher's contract precludes this, but defense in depth) is silently skipped at query time rather than failing the whole query.
- The publisher is a zip Lambda while the extractor is a container image, matching the uploader's reasoning in ADR-0010. Packaging follows dependency weight, not consistency for its own sake.
- The 1 GB per-query scan cap is a starting point. Raising it for a specific operator query is a workgroup override; raising the default is a tfvar.

## Alternatives considered

- **Extractor writes directly to S3 (skip Streams).** Rejected—adds a hot-path S3 `PutObject` to every extraction, with its own IAM grant and a dual-write failure mode (DDB succeeds, S3 fails, or vice versa). The Stream is asynchronous and absorbs the failure; the extractor's hot path stays one DDB call as ADR-0009 designed.
- **SNS instead of Streams as the egress mechanism.** Rejected—would require the extractor to `Publish` in addition to its DDB write, doubling the failure surface and pushing fan-out (which the consumer does not need) into the egress path. Streams is the natural exit from the conditional terminal write.
- **One bucket for ingestion + extractions under different prefixes.** Rejected—different access patterns (read-once vs. queried-on-demand), different lifecycle requirements (one transitions to cold storage, the other cannot), different IAM grants. One bucket per concern is cleaner and the cost of an additional bucket is zero.
- **Glue crawler instead of a static catalog table.** Rejected—the result schema is project-owned and stable; a crawler reproduces something we already know on a schedule we would have to operate, with its own IAM surface and cost.
- **`MSCK REPAIR TABLE` on a schedule (or fired by S3 PUT) instead of partition projection.** Rejected—projection eliminates the periodic-job / event-driven catalog-maintenance class of bugs entirely. The day range is a one-line change when 2030 approaches.
- **Athena federated query directly against DynamoDB.** Rejected—adds the Athena DynamoDB connector Lambda, scans DDB on every query, and forfeits the "results are artifacts" property that ADR-0011 exists to enable.
- **Parquet instead of JSON in the analytics partition.** Deferred—Parquet is the right answer for analytics-cost reasons at scale, but it requires a transform on the publisher's write path and breaks the "the result object the caller reads is the same bytes Athena queries" property. At portfolio volumes the storage and scan-cost savings do not justify the complexity. Reopen if scan costs become material.
- **Lifecycle transition to IA or Glacier on the analytics bucket.** Rejected—Athena cannot transparently query Glacier objects. Cold-tier transitions would silently break the query layer without any signal at the bucket level.
- **Unbounded Athena workgroup.** Rejected—an unbounded workgroup is one bad query away from a surprise bill. The scan-cutoff is the default budget boundary; raising it is a deliberate opt-in.
- **No DLQ on the publisher event source mapping.** Rejected—without it, a failing batch after `maximum_retry_attempts` would be discarded silently. The DLQ is the single source of truth for failed batches, mirroring ADR-0005's queue posture.
