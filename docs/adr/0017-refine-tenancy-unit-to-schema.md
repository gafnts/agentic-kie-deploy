# ADR-0017: Refine the Tenancy Unit to the Schema, Not the Caller

## Status

Accepted (2026-06-15). Amends [ADR-0013](0013-single-tenant-deployment-model.md).

## Context

ADR-0013 settled the single-tenant, per-instance deployment model and rejected a multi-schema platform. Both still hold. But its Decision section fixed the tenant to "exactly one upstream caller (one IAM principal)," conflating the unit of tenancy with a single caller. That proxy is too tight.

The architectural lock is the extractor image ([ADR-0009](0009-extractor-lambda.md)): one document type, one schema, one set of prompts and validators. Nothing else binds an instance to a single principal. The uploader route is open to any in-account caller ([ADR-0010](0010-uploader-module.md)), and the result prefix is readable by any granted consumer ([ADR-0011](0011-s3-as-result-delivery.md)), as ADR-0013's own third decision bullet already allows. The boundary that actually exists is the schema.

## Decision

The tenancy unit is the **schema (the extraction use case)**, not the caller. One instance serves one document type, owned and operated by the team that deploys it. Within that boundary:

- **Callers may be many.** Multiple in-account upload paths (a UI, an automated pipeline, a batch job) can feed one instance, provided each goes through the presigner so the document-id key contract ([ADR-0006](0006-document-id-lifecycle.md)) holds.
- **Consumers may be many.** Event-driven subscribers on the result prefix and ad-hoc readers on the Glue/Athena surface coexist, widening ADR-0013's "additional readers" note into a first-class topology.

Single-tenant and the platform rejection are unchanged: multi-tenant still means multiple schemas in one deployment, which the extractor image deliberately cannot serve. This promotes ADR-0013's deferred "single-document, multi-caller (shared instance)" alternative to accepted; the cross-caller concerns it named (concurrency contention, cost attribution) are intra-tenant operational matters for the owning team, not a breach of tenancy.

## Consequences

- The invariant is now stated where it lives (the schema), so multi-caller and multi-consumer topologies stop contradicting the docs and the architecture diagram.
- The decoupling the async S3-sink buys (producers and consumers that never know each other) is a documented property rather than an accident.
- Concurrency budgeting, cost attribution, and any per-caller rate limiting are the owning team's concern; the principal-level hardening lever (a Lambda authorizer) is unchanged and still deferred ([ADR-0010](0010-uploader-module.md)).
- ADR-0013's "exactly one upstream caller" wording is superseded by this ADR; its title, deployment model, and platform rejection are not.
