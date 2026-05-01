# ADR-0003: Four-Layer Ingestion Bucket Hardening

## Status

Accepted (2026-05-01)

## Context

The ingestion bucket is a public-facing entry point: it receives documents from untrusted clients via pre-signed PUT URLs. Without explicit controls, S3's legacy permission model creates several distinct attack surfaces — public grants via ACLs or bucket policies, ambiguous object ownership when uploads come from external parties, HTTP fallback on pre-signed requests, and unencrypted data at rest. Each surface requires a different mechanism to close, and closing one does not close the others.

## Decision

Lock down the bucket through four orthogonal mechanisms:

- **Public Access Block** — all four flags enabled (`block_public_acls`, `block_public_policy`, `ignore_public_acls`, `restrict_public_buckets`). Blocks public-grant attempts at the account/bucket boundary; no ACL or bucket policy can accidentally make objects public.
- **Ownership controls (`BucketOwnerEnforced`)** — disables ACLs entirely. Every object is owned by the bucket account regardless of who uploaded it, collapsing the legacy ownership question and removing ACLs as a permission path.
- **TLS-only bucket policy** — denies any request where `aws:SecureTransport = false`. Pre-signed URLs are HTTPS by default, but the policy is the enforcement layer: an old SDK or a misconfigured client cannot fall back to HTTP.
- **Default encryption (SSE-S3 / AES256)** — protects data at rest. Key management is handled transparently by AWS. See ADR-0004 for the encryption strategy decision.

Together, these leave only IAM identity policies and the bucket policy as the access-control surface.

## Consequences

Positive:
- Access-control surface is minimal and explicit; every remaining permission path is IAM-based
- No ACL or bucket policy can accidentally make objects public, even if one is misconfigured
- Object ownership is unambiguous regardless of the uploading principal
- HTTP fallback is impossible even with valid credentials or an old SDK

Negative:
- The TLS-only policy applies to authenticated requests too, so verifying it requires credentials over HTTP — an anonymous probe cannot distinguish a `SecureTransport` deny from a missing-auth deny
- `BucketOwnerEnforced` is irreversible on a bucket that has existing ACL-controlled objects; not a concern here since the bucket is new

Neutral:
- Each layer closes a different door; the mechanisms are independent — removing any one leaves a specific gap without breaking the others

## Verification

The TLS-only policy applies to authenticated requests as well as anonymous ones, so the test must use credentials over HTTP — otherwise a 403 could come from missing auth rather than the `SecureTransport` condition.

```bash
# Negative test — authenticated PUT over HTTP, should fail with AccessDenied.
aws s3 cp test.pdf s3://<bucket>/test.pdf \
  --endpoint-url http://s3.us-east-1.amazonaws.com

# Positive test — same call over HTTPS, should succeed.
aws s3 cp test.pdf s3://<bucket>/test.pdf
```

An anonymous probe confirms the Public Access Block is enforced independently of TLS:

```bash
# Should return 403 regardless of TLS — denied at the Public Access Block layer.
curl -X PUT "http://s3.amazonaws.com/<bucket>/test.pdf" --data-binary @test.pdf
```

## Alternatives considered

- **ACL-based access control**: rejected — ACLs are a legacy model; `BucketOwnerEnforced` removes the ownership ambiguity and eliminates ACLs as a permission path entirely.
- **Relying on pre-signed URL HTTPS default without a TLS policy**: rejected — the default can be overridden by a misconfigured client or old SDK; the bucket policy is the only layer that enforces it unconditionally.
- **Bucket owner preferred (instead of enforced)**: rejected — still allows uploaders to retain object ownership, which leaves an ambiguous access-control surface.
