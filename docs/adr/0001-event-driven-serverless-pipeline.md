# ADR-0001: Event-Driven Serverless Extraction Pipeline

## Status

Accepted (2026-05-01)

## Context

This repository is the production infrastructure layer for [`agentic-kie`](https://github.com/gafnts/agentic-kie), a Python library that extracts structured fields from PDF documents using LLMs. The library exposes two strategies — single-pass (one structured LLM call over the full document) and agentic (a ReAct loop with document tools) — behind a shared `Extractor` protocol, so strategies are independently swappable without changing downstream code.

A benchmark on the [Kleister NDA](https://github.com/applicaai/kleister-nda) corpus (540 SEC-filed NDAs, 83-document dev partition) compared both strategies across three model providers and two capability tiers. Single-pass extraction matches or beats the agentic approach across the entire lite tier; at the standard tier, the strategies converge and any agentic improvement is too small to justify a 2–4× cost and latency penalty. The winning configuration — Gemini Standard (single-pass) — reaches **91.5% F1** at roughly **$0.007 per document** and under **ten seconds of latency**.

The infrastructure must solve four constraints the library does not address: absorbing arbitrary document uploads without proxying large payloads through compute, decoupling the synchronous client interaction from the slow LLM call, making extraction retryable without re-running the upload, and fitting heavier ML and LLM dependencies into a Lambda execution environment.

## Decision

Use a fully serverless, event-driven, asynchronous pipeline on AWS:

1. A presigner Lambda behind API Gateway returns a short-lived pre-signed S3 PUT URL to the client.
2. The client uploads the document directly to S3, bypassing API Gateway payload limits.
3. S3 emits an `Object Created` event to EventBridge, which routes it to an SQS queue with a dead-letter queue and redrive policy.
4. SQS triggers the extractor Lambda, packaged as a container image from ECR to accommodate the `agentic-kie` library's dependencies.
5. The extractor runs the `agentic-kie` library (defaulting to single-pass) and writes the structured result to DynamoDB, keyed by document ID.

The entire pipeline is provisioned with Terraform, using a remote S3 backend with native state locking, organized as small per-concern modules (storage, queue, table, registry, extractor, uploader).

## Consequences

Positive:
- Client interaction is synchronous and cheap — just a URL handoff, no payload proxied through compute
- Extraction is fully decoupled from the upload; a failure in the extractor does not require re-uploading
- DLQ + redrive policy makes every extraction attempt retryable without manual intervention
- Container image removes Lambda layer size constraints for ML and LLM dependencies
- Every component scales to zero when idle

Negative:
- No synchronous result path — the client cannot receive extraction results from the upload call itself
- More moving parts than a single synchronous API; operational surface is wider

Neutral:
- The choice of extraction strategy (single-pass vs. agentic) is a library-level concern and can be changed independently of the infrastructure

## Alternatives considered

- **Synchronous Lambda via API Gateway**: rejected — API Gateway has a 10 MB payload limit and a 29-second integration timeout; both are violated by realistic documents and LLM extraction latency.
- **SNS instead of SQS**: rejected — SNS has no built-in retry queue or DLQ; failed deliveries are harder to inspect and redrive.
- **Always-on ECS or EC2**: rejected — eliminates scale-to-zero, raises idle cost, and adds container orchestration overhead for a workload that is inherently bursty.
