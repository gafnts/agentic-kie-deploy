# ADR-0011: S3 as Result Delivery

## Status

Proposed (2026-05-24). Supersedes ADR-0002.

## Context

ADR-0001 fixed the pipeline as asynchronous: the extractor writes to DynamoDB but does not deliver results back to the caller. ADR-0002 deferred a polling endpoint (`GET /results/{doc_id}`) as the result-delivery channel, listing webhook and WebSocket as valid future alternatives. At the time the caller was unspecified—neither the integration shape nor the consumption pattern was settled.

The integration shape is now settled. The caller is an internal AWS service: a workflow engine, a Lambda, an EventBridge consumer. It runs in the same account, has its own IAM role, and wants to react to results rather than poll for them. That shifts the calculus around result delivery in ways that make ADR-0002's deferral the wrong answer in the new model—not because the deferral was wrong at the time, but because the constraint set changed.

For an AWS-native caller, three options dominate:

| Option | Mechanism | What the caller does |
|---|---|---|
| Polling endpoint | `GET /results/{doc_id}` Lambda reads DynamoDB | Calls the endpoint on a loop until status is terminal |
| SNS topic | Extractor (or downstream) publishes to a topic; caller subscribes | Receives a message per result; needs an HTTPS or queue subscription |
| S3 result object | Extractor (or downstream) writes JSON to a known S3 address; caller subscribes to `s3:ObjectCreated:*` on the prefix | Receives an EventBridge event; reacts with a Lambda, Step Functions task, or Pipes consumer |

The caller knows `document_id` from the presign response (ADR-0006), which means it knows the result address before the result exists. That is load-bearing: it lets the caller install an event subscription on the exact key ahead of time, and the trigger fires on first write—not on a poll loop, not on a webhook delivery, but on the result object itself coming into existence.

## Decision

S3 is the result sink. The extractor's terminal write to DynamoDB fans out to S3 via DynamoDB Streams (the mechanism is owned by ADR-0012); the consumer Lambda writes the result payload to:

```
s3://{analytics_bucket}/extractions/{yyyy}/{mm}/{dd}/{document_id}.json
```

The caller subscribes to `s3:ObjectCreated:*` on the `extractions/` prefix (filtered as narrowly as it wants) via its own EventBridge rule or bucket-notification target, and consumes the event with whatever AWS-native pattern fits its workflow: a Lambda, a Step Functions `waitForTaskToken` resumption, an EventBridge Pipes target, an SQS queue.

No polling endpoint. No SNS topic. No webhook.

The result payload is the same shape as the DynamoDB item it derives from—`document_id`, `status`, `created_at`, `completed_at`, `extracted_fields`, `confidences`, `model_version`, `token_usage`, `processing_ms`, `error` (on failure). The S3 object is the artifact: it is what the caller reads, it is what Athena queries (ADR-0012), and it is what a future audit reads. There is one source of truth for the answer at the consumption boundary.

### Why S3 wins for an AWS-native caller

- **The trigger is native infrastructure on both sides.** S3 → EventBridge → the caller's rule. No HTTPS server to expose, no polling loop to engineer, no message broker to operate. Every AWS-native integration pattern (Lambda, Step Functions, Pipes, EventBridge Scheduler) consumes S3 events as a first-class event source.
- **`waitForTaskToken` is the killer integration.** A Step Functions workflow that needs an extraction result can call the presigner, upload, suspend on `waitForTaskToken`, and resume when the S3 event fires—no polling, no callback URL, no compensation logic for missed deliveries. The S3-as-sink choice is what makes that pattern available; polling and webhook do not.
- **The caller learns the address before the result exists.** Because `document_id` is returned at presign (ADR-0006) and the result address is derived from it, the caller installs its event subscription as part of upload setup. There is no race where the subscription is missing when the result arrives—the subscription is on the *key*, not on the *creation*, and S3's event model fires the first time the key exists.
- **The result and the analytics partition are the same bytes.** The caller's consumer and the Athena workgroup (ADR-0012) read the same S3 object. There is no second copy to keep consistent, no derived index to rebuild, no consistency gap to reason about.
- **Cost is small and predictable.** A `GetObject` per result is cents per million; an S3 PUT per result is the same. No per-poll Lambda invocation, no per-message SNS charge, no API Gateway request charge.

### Why not polling

ADR-0002's reasoning held for an unspecified caller. For an AWS-native caller it does not:

- A polling loop forces the caller to either run a scheduled Lambda or carry a long-running poller, both of which are anti-patterns next to a native event source.
- The Lambda + API Gateway + DDB read on every poll is pure infrastructure on our side, scaling with the caller's poll frequency rather than with the actual rate of work.
- The integration patterns the caller actually wants to use (Step Functions, EventBridge, Pipes) are awkward or impossible to drive from a polling loop.

### Why not SNS

- SNS buys fan-out to multiple subscribers per message. There is one consumer; the fan-out is unused.
- If multiple consumers ever appear, the right place for fan-out is on the consumer's side (their SNS topic, their EventBridge bus subscribed to the S3 event), not on ours. Keeping the topology one-to-one on our side keeps our blast radius small.
- SNS-to-Lambda is at-least-once with a 256 KB message-size cap; while the result payload comfortably fits today, putting it in the message rather than referencing it sets up a future cliff we do not need to set up.
- The caller is responsible for the subscription either way (SNS subscription or S3 event rule), but the S3 event subscription gives the caller the payload at a known address as a side effect, not as a separate fetch.

### Why not webhook

- Requires the caller to expose an HTTPS endpoint. AWS-native callers prefer to *not* run an HTTPS server for inbound traffic—that is the failure mode SNS-HTTPS subscriptions try to paper over and consistently struggle with.
- Retries, signing, and DLQ on webhook failures become our responsibility. With S3 events, retries are EventBridge's job and they work without configuration on either side.
- Webhook is the right answer when the caller is *not* in AWS. Inside AWS, every webhook setup is reproducing a worse version of what EventBridge gives for free.

### Why not WebSocket

- Real-time delivery is appealing but the operational cost (connection registry, API Gateway Management API permissions, connection lifecycle) is unjustified when the caller is reacting to an event, not a stream. The same argument that ADR-0002 made still holds, more strongly now that the caller pattern is clear.

## Consequences

Positive:

- Caller can use any AWS-native integration pattern: Lambda, Step Functions, EventBridge Pipes, SQS. The architecture does not constrain the consumption shape.
- No per-caller infrastructure on our side. Onboarding a consumer is an S3 read grant and an event subscription on the consumer's side; we ship nothing.
- The result object, the analytics partition, and the audit record are the same bytes. One source of truth at the consumption boundary.
- Caller knows the result address at presign time, so subscriptions can be installed before the result exists. No race, no missed deliveries.
- The extractor's hot path is unchanged from ADR-0009—one DynamoDB write. The hop from DDB to S3 is asynchronous, owned by the results module (ADR-0012), and out of the extractor's failure surface.

Negative:

- Delivery latency is the extractor's terminal write plus Stream propagation (hundreds of ms) plus consumer Lambda invocation. Not synchronous; not appropriate for a caller that needs the result inside a single API response.
- The caller needs `s3:GetObject` on the `extractions/` prefix. The grant is narrow and explicit but it is one more IAM line on the caller's side.
- The DynamoDB Streams + consumer Lambda chain is now in the critical path for result delivery. A failure there delays delivery even though the extraction itself succeeded. Mitigated by Streams' 24-hour retention and the consumer's on-failure DLQ (ADR-0012).

Neutral:

- A polling endpoint is not foreclosed. If a future non-AWS caller ever needs one, it is one additive Lambda—`GET /results/{doc_id}` returns a presigned `GetObject` URL for the S3 result object. The result address is the same; only the discovery channel differs.
- DynamoDB is no longer the read path for results at the consumption boundary. It remains the system of record for the extractor's idempotency claim and the Stream source for the result publisher; consumers read S3. The full implications for ADR-0007's framing are captured in its update.

## Alternatives considered

- **Polling endpoint (ADR-0002's choice).** Rejected for an AWS-native caller—adds infrastructure on our side for a consumption pattern (poll loop) that does not fit the integration patterns the caller actually wants to use.
- **SNS topic with subscribers per consumer.** Rejected—fan-out is unused with one consumer; the 256 KB message cap is a future cliff; the consumer ends up doing the same event-handling work as in the S3 case but against a less first-class trigger.
- **Webhook to a caller-supplied URL.** Rejected—requires the caller to expose an HTTPS endpoint and pushes retry/signing/DLQ ownership to our side. Appropriate for non-AWS callers; obsolete inside AWS.
- **API Gateway WebSocket.** Rejected—same reasoning as ADR-0002; the operational cost of connection state is unjustified for an event-shaped consumption pattern.
- **Result payload returned inline from the extractor (synchronous).** Rejected—settled in ADR-0001; revisiting here would undo the asynchronous pipeline.
- **DynamoDB row as the read path with the caller doing `GetItem` directly.** Rejected—couples the caller to our schema, requires a DDB read grant on the caller side, and gives up the analytics-partition property (the same bytes serve consumption and Athena). The S3 result object is the cleaner contract surface.

## Supersession

This ADR supersedes ADR-0002. The historical reasoning in ADR-0002 holds for the integration model it assumed; the integration model changed, and the right answer changed with it. The webhook and WebSocket alternatives ADR-0002 listed as future options are subsumed here—they were rejected under the new model for the reasons above.
