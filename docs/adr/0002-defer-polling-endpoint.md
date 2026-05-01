# ADR-0002: Defer Result Delivery to a Polling Endpoint

## Status

Accepted (2026-05-01)

## Context

ADR-0001 established a fully asynchronous pipeline. The extractor Lambda writes structured results to DynamoDB but does not deliver them back to the client. In an SQS-triggered Lambda, the return value is invisible to the caller — SQS only inspects it for ack/nack purposes — so returning results requires an explicit delivery mechanism added on top of the existing pipeline.

Three options are available:

| Option | Mechanism | Tradeoff |
|---|---|---|
| Polling endpoint | `GET /results/{doc_id}` Lambda reads DynamoDB | Stateless, additive, no changes to existing components |
| Webhook / callback | Client supplies a callback URL; extractor POST-s results there | No new infrastructure, but requires the client to expose a reachable HTTPS endpoint |
| WebSocket | Client holds an open API Gateway WebSocket connection | Real-time, but requires a connection registry and API Gateway Management API permissions |

The current scope covers the ingestion and extraction path. No client has been integrated yet, and the delivery mechanism is a downstream concern.

## Decision

Defer the polling endpoint. DynamoDB remains the sole result sink for now.

When the endpoint is built, it will be a `GET /results/{doc_id}` route on the existing API Gateway, backed by a new reader Lambda that reads directly from DynamoDB. The prerequisite is that `doc_id` is predictable and surfaced to the client at upload time — derived from the S3 object key so the client has something to poll with before any extraction has completed.

## Consequences

Positive:
- No scope creep into the current implementation phase
- DynamoDB-as-sink is already implemented unconditionally; no extractor changes are needed when the endpoint is added
- The polling endpoint, when built, is a pure additive change: one new Lambda, one new API Gateway route, no modifications to the extractor, queue, or event routing

Negative:
- Clients cannot retrieve results until the polling endpoint is implemented
- The `doc_id` derivation convention must be established and held stable before any client integration begins

Neutral:
- Webhook and WebSocket remain valid future alternatives if push delivery becomes a requirement

## Alternatives considered

- **Webhook / callback URL**: deferred — clean for server-to-server use cases, but requires every client to expose a reachable HTTPS endpoint, which is a strong assumption for the initial integration.
- **WebSocket via API Gateway**: deferred — real-time delivery is appealing but the operational overhead (connection registry table, API Gateway Management API permissions, connection lifecycle management) is disproportionate to the current scope.
