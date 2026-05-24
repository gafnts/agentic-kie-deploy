# ADR-0006: Server-Generated Document ID, Carried by the S3 Object Key

## Status

Accepted (2026-05-08)

## Context

Several modules need a stable identifier for each ingested document. One component must mint it and return it to the client before the upload, while the rest of the pipeline must be able to recover it without an extra channel.

## Decision

The **presigner Lambda** mints the document ID. It is a UUIDv7, returned to the client in the presign response together with the presigned PUT URL.

The S3 object key embeds the ID directly:

```
uploads/{yyyy}/{mm}/{dd}/{document_id}
```

Because the presigned URL pins the exact object key, the client cannot rewrite the upload to a different ID. From that point on the ID rides the existing event flow:

| Stage                    | How `document_id` is available                            |
| ------------------------ | --------------------------------------------------------- |
| Presigner → client       | Returned in the presign response body                     |
| Client → S3              | PUT goes to the key the URL was signed for                |
| S3 → EventBridge         | `Object Created` event includes `object.key`              |
| EventBridge → SQS        | Event payload forwarded verbatim                          |
| SQS → Extractor          | Lambda parses `document_id` out of `s3.object.key`        |
| Extractor → DynamoDB     | `PutItem` with PK = `document_id`                         |
| Future poller → DynamoDB | `GetItem(document_id)` using the ID returned at presign   |

UUIDv7 is preferred over UUIDv4 because it is time-sortable; if a GSI on creation order is ever added, the partition key sorts naturally without a separate timestamp attribute.

### Contract

- **Format**: UUIDv7, lowercase, hyphenated (36 characters).
- **Key shape**: `uploads/{yyyy}/{mm}/{dd}/{document_id}`. The date prefix is for human-readable bucket browsing and S3 request-rate partitioning; it is not authoritative—only the `document_id` segment is.
- **Authority**: the presigner is the only writer of new IDs. The extractor never invents an ID; if it cannot parse one out of the object key, it acks the message—a malformed key is a poison-pill, not a retryable failure (ADR-0009).

### Module responsibilities

- **Presigner**: generate the UUIDv7, build the object key, sign the PUT URL against that exact key, return `{ document_id, upload_url, expires_at }`.
- **Storage**: documents the key prefix convention in its README; no code change.
- **Table**: declares PK `document_id` (string), references this ADR in its variables/README so the contract is visible at the schema boundary.
- **Extractor**: parses `document_id` from `s3.object.key`; writes to DynamoDB with a conditional expression (`attribute_not_exists(document_id)` or a status-state-machine `UpdateItem`) to absorb SQS at-least-once redelivery.

## Consequences

Positive:

- One identifier, generated once, carried by infrastructure the pipeline already has. No registry, no sidecar metadata, no extra hop.
- The presigned URL pinning the object key makes "server-controlled ID, client-untrusted" enforceable at the S3 layer rather than at application logic.
- UUIDv7's time-ordering keeps a future creation-order GSI cheap.
- A single correlation ID across CloudWatch logs, X-Ray traces, S3 keys, SQS messages, and the DynamoDB row makes debugging tractable.

Negative:

- The S3 key format becomes a contract between presigner and extractor. Changing it later requires coordinated deploys or a parser that accepts both shapes during the transition.
- The `document_id` is a guessable-shaped opaque pointer, not a secret. Any future read endpoint must enforce its own authorization—knowing a `document_id` must not by itself authorize reading the result.

Neutral:

- UUIDv7 leaks creation time. For this workload that is acceptable and arguably useful; for workloads where it is not, UUIDv4 is a drop-in replacement with no schema change.

## Alternatives considered

- **Client-supplied ID**: rejected—no server control, collision and overwrite risk, no way to enforce uniqueness without an extra lookup on every presign request.
- **Extractor-generated ID**: rejected—the client never learns the ID, so no polling is possible without a synchronous callback channel that defeats the asynchronous design from ADR-0001.
- **S3 version ID as primary key**: rejected—version IDs are opaque, S3-internal, and only known after the upload completes; the client cannot receive one at presign time.
- **UUIDv4 instead of UUIDv7**: viable, and the right choice if creation-time leakage is ever a concern. UUIDv7 is preferred here for its sortability.
