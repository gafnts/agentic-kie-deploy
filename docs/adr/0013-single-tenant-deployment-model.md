# ADR-0013: Single-Tenant Deployment Model

## Status

Proposed (2026-05-24)

## Context

The system's shape—presigner, extractor, table, results publisher, analytics bucket—could plausibly be delivered to its consumers in two different ways:

- **Template / per-instance.** The Terraform modules under `infra/modules/` are the unit of reuse. A team that wants a document-extraction pipeline deploys an instance: bucket, queue, table, extractor image baked for their document type, presigner, results publisher, analytics bucket, alarms—all stamped with their environment prefix. Multiple document types means multiple deployments.
- **Multi-tenant platform.** One shared deployment routes uploads by document type (and possibly by tenant). The extractor dispatches dynamically on document type, the table partitions per tenant, an authorizer enforces per-caller rate limits, a registry maps document type → extraction configuration. One operational footprint serves many teams.

Earlier ADRs implicitly assumed the first model without naming it. This ADR settles which model the system commits to, so the implicit becomes the explicit and future readers do not have to reverse-engineer it from the IAM model and the extractor's image.

The extractor (ADR-0009) is the constraint that decides it. The container image carries:

- The `agentic-kie` configuration for one specific document type (prompts, target schema, validators).
- The model identifier (`LLM_MODEL`) and the secrets path (`LLM_PROVIDER_SECRET_ARN`).
- The memory, timeout, and ephemeral-storage sizing tuned for that workload.

The DynamoDB schema (ADR-0007) is the answer shape for one document type. The S3 result address (ADR-0011) is one key shape that one consumer subscribes to. The IAM model on the uploader (ADR-0010) is open within the account because the assumption is the caller set is small, known, and coordinated. All of those assumptions hold together cleanly if the deployment shape is per-(caller, document type), and each of them needs to be re-litigated if the deployment shape is multi-tenant.

| Model                   | Operational cost per team           | Cross-team coupling                                         | What changes per document type            |
| ----------------------- | ----------------------------------- | ----------------------------------------------------------- | ----------------------------------------- |
| Template / per-instance | One Terraform `apply`, cents to single dollars/mo idle | None                                            | A new instance (full module set)          |
| Multi-tenant platform   | Shared infra + per-tenant config    | Shared blast radius, noisy neighbors, shared limits         | A registry entry + a deploy of the dispatch logic |

The per-instance cost looks higher in dollar terms but vanishes against the multi-tenant tax—extractor dispatch, per-tenant rate limits, isolation in keys and table partitions, schema registry, per-tenant cost attribution, version skew between tenants. Every one of those is a critical system in a multi-tenant platform and a non-feature in a per-instance template.

## Decision

The pipeline is a deployable template, not a platform. Each deployed instance:

- Has exactly **one upstream caller** (one IAM principal that invokes the uploader API).
- Processes exactly **one document type** (one extractor image with its document-type configuration baked in).
- Has exactly **one downstream consumer** (one entity subscribing to the result S3 prefix).

Multi-document support is achieved by deploying multiple instances of the modules, not by extending an instance. A team that needs to extract three document types deploys three instances; each is isolated end-to-end (bucket, queue, table, extractor, presigner, results publisher, analytics bucket, alarms).

The Terraform modules are the unit of reuse. The deployment (one root `terraform apply` per environment) is the unit of tenancy. A caller can use many instances—one per document type it needs—but an instance has exactly one caller.

### Access control: two perimeters

Access control on the pipeline is the composition of two layers, each weak on its own but airtight together:

1. **Account-level IAM.** Only principals inside the same AWS account can `execute-api:Invoke` the uploader route. The cross-account boundary is the outer perimeter.
2. **Document-type coupling.** An in-account principal that successfully signs a request and uploads a document still produces useless output if the document type does not match what the extractor was built for: the extraction fails or returns garbage, the result object lands in the wrong shape, and the downstream consumer (which knows the expected shape) discards it.

The combination is the security model. Account IAM keeps outsiders out; document-type coupling makes accidental in-account access self-defeating.

If, in a given environment, the account boundary is *not* a sufficient perimeter—multiple unrelated teams in one account, an account that hosts unrelated workloads, a regulated environment that needs principal-level allowlisting—the available hardening lever is a Lambda authorizer on the uploader route that checks the request context's principal ARN against an allowlist. The shape of that change is recorded in ADR-0010's "Negative" consequences and alternatives. It is deferred until the account boundary stops being sufficient; the default deployment shape (one well-known caller in a dedicated or coordinated account) does not need it.

### Implications for the existing modules

| Module       | What "single-tenant" looks like                                                                                                                                                |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `uploader`   | One IAM principal expected to invoke `POST /uploads`. CloudWatch's `client_principal` should be the same value across calls in a healthy state; a different principal is an investigative signal. |
| `extractor`  | One container image, one document-type configuration, one secret path. The image identity *is* the instance's identity.                                                        |
| `table`      | One results table per instance. The `extracted_fields` map shape is determined by the extractor's configuration; no cross-instance schema reuse is needed or attempted.        |
| `results`    | One S3 result prefix, one expected consumer subscribed to it. The Glue table and Athena workgroup are per-instance—cross-instance analytics is a future federation problem, not an in-instance problem. |
| `iam`        | The `Environment`-tagged deny guard is the per-env boundary inside an instance; it does not become a per-tenant boundary, because there is no tenancy concept inside an instance. |

### Why not a platform

The platform model adds real value when:

- The compute is expensive and shared (e.g., a GPU pool, an embedding cluster). Lambda + LLM API calls are neither.
- Data needs to be queried across tenants. The use case here is per-document-type extraction, not portfolio analytics.
- Governance must be centralized. Per-instance deployments are themselves a governance pattern—the platform team owns the module versions; product teams own their instances.

For document extraction specifically, document types are heterogeneous by nature: schemas, prompts, validation rules, sometimes provider and model choices differ per type. A platform would have to make all of those dynamic (registry, per-document-type config storage, dispatch). That work earns nothing because the extractor image is already the natural carrier for that configuration in the per-instance shape.

The platform model also makes blast-radius reasoning harder: one bad deploy affects every tenant; one noisy tenant consumes shared concurrency; per-tenant rate limits become a system the platform must own (the deferred Lambda authorizer in ADR-0010 is exactly that system, named). At portfolio scale, none of those problems is worth solving in advance.

## Consequences

Positive:

- Every earlier ADR's implicit assumption (one caller, one document type, one consumer) is now an explicit contract. Future readers see the deployment shape without having to reverse-engineer it.
- Onboarding a new document type is a `terraform apply` of an additional instance—no shared code path to extend, no registry to update, no migration of existing tenants.
- Blast radius is bounded by the instance: a misconfigured extractor, an exhausted concurrency cap, or a corrupted result table affects exactly one (caller, document type) pair.
- The security model is articulable in one sentence: account IAM plus document-type coupling.
- The Terraform modules become a versioned, distributable artifact—the same shape as a Terraform registry module. Cross-team reuse happens at the module level, not at the deployment level.

Negative:

- Per-instance idle cost is duplicated across instances. At portfolio scale this is cents to single dollars per instance per month (DynamoDB on-demand, Lambda billed at usage, S3 storage proportional to actual data). Real, but small.
- Cross-document-type analytics is not in scope. An organization that wants to query "all extractions across all document types" must federate at the Glue/Athena layer across instance buckets. Deferred until the need is real.
- Common module upgrades require N deploys (one per instance). Mitigated by the deploy being mechanical (a tfvar bump) and by the modules being internally versioned by their git ref.

Neutral:

- A future multi-tenant platform is not foreclosed. The right shape, if and when it is needed, is a thin platform layer that vends instances of these modules (think AWS Service Catalog, or a Terraform module registry with a small enrollment workflow). The modules themselves do not need to change.
- The "one consumer" property on the result side is one-sided: the caller is responsible for fan-out if it has multiple downstream consumers. Its SNS, EventBridge bus, or Step Functions topology is the right place for that fan-out, not ours.

## Alternatives considered

- **Multi-tenant platform with dynamic dispatch.** Rejected—the extractor's document-type coupling is the architectural lock that makes per-instance simple and platform-mode complex. The platform model would require building the dispatch, registry, per-tenant rate limiting, and isolation systems that the per-instance model gets for free from AWS.
- **Multi-document, single-caller (shared instance).** Considered—one caller uploads multiple document types and the extractor branches on a `document_type` field in the upload metadata. Rejected: the extractor's image carries the prompts and schemas; loading multiple sets at runtime turns the extractor into a multi-tenant system internally, with the same complications scoped to one Lambda. Not better than separate instances.
- **Single-document, multi-caller (shared instance).** Considered—one document type, multiple internal callers in the same account share an instance. Viable today (the IAM model already allows it), but introduces cross-caller concerns (noisy neighbors on concurrency, shared rate limits, shared cost attribution) that the per-instance model avoids. Deferred unless a use case appears; document-type coupling means this only ever serves N teams who all want the *same* document type extracted, which is unusual.
- **Absorb the contract into ADR-0001.** Rejected—ADR-0001 is about the pipeline's runtime shape (asynchronous, event-driven, serverless). The deployment model is a separate axis and deserves its own ADR so future readers find it without inferring it from a paragraph buried in the foundational ADR.
- **Lambda authorizer restricting the caller principal at MVP.** Deferred—the account boundary is the assumed perimeter at this scale; tightening to a specific principal is one Terraform change away if the boundary stops being sufficient. The lever is documented in ADR-0010 so the future change is low-friction.
