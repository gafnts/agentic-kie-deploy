# ADR-0014: Split the Results Module into Publisher and Analytics

## Status

Accepted (2026-05-31). Supersedes ADR-0012.

## Context

ADR-0012 bundled three concerns into one Terraform module at `infra/modules/results/`:

1. **The publisher**. The DynamoDB Streams consumer Lambda (plus its DLQ, IAM, and alarms) that fans terminal rows out to S3.
2. **The analytics store**. The `extractions` bucket (plus its access-log sibling) that holds the result objects.
3. **The query layer**. The Glue database, the projected `extractions` table, and the dedicated Athena workgroup (plus its query-results bucket).

That ADR explicitly rejected splitting these, on the grounds that they "share one contract, the partition shape", and that separate modules would mean keeping that contract in lockstep across multiple places. Bundling kept the contract co-located.

The bundling decision has not aged well, for four reasons the original ADR did not weigh:

- **"results" is overloaded.** The DynamoDB table is already `…-results` (the system of record, ADR-0007), the README calls it the "Results table," the Glue database is `…_results`, and the Athena workgroup is `…-results`. The module being `results` too means the word names four distinct things. The most stateful, most important of them—the table—has the strongest claim to the name.

- **It is the only non-single-concern module.** The README states the infrastructure is "organized as small, per-concern Terraform modules." Every sibling is a crisp noun—`uploader`, `bucket`, `queue`, `table`, `extractor`, `alarms`. `results` is the lone three-in-one.

- **The codebase already speaks "publisher" and "analytics."** The Lambda is `…-publisher`, its handler is `publisher.handler`, its source is `publisher.py`, and the README calls it the "Result publisher." The bucket is surfaced as `analytics_bucket_name`/`analytics_bucket_arn` in the root outputs and described as the "analytics bucket." Only the module wrapper and ADR-0012 say "results." The names a split would use are already the project's vocabulary; only the packaging lags.

- **The co-location argument never actually held.** The "one shared contract" ADR-0012 bundled to protect is the partition shape `extractions/{yyyy}/{mm}/{dd}/{document_id}.json`. It is *already* a literal in three places that already span two languages—`compose_key()` in `src/results/publisher.py`, the `s3:PutObject` scope in `publisher.tf`, and the `storage.location.template` in `athena.tf`. Co-locating them in one module never unified them; they were three literals across Python and HCL the whole time. A module boundary does not make that coupling looser or tighter—it only changes which directory each copy lives in.

Two further facts make now the right time:

- **Lifecycle mismatch.** The analytics store is stateful and rarely changes—it holds the durable record (no expiration, `force_destroy` gated off in prod). The publisher is stateless and churny—it redeploys whenever the payload shape or a tuning lever changes. Bundling them means every publisher tweak re-plans the bucket, Glue, and Athena resources, and the irreplaceable bucket shares a module with a disposable Lambda.

- **Nothing is in a protected environment yet.** There is no prod deployment, and staging is `force_destroy`-enabled. Splitting now costs nothing in downtime or data migration; splitting after prod would require state surgery.

## Decision

Replace the single `results` module with two per-concern modules: `publisher` (the feed) and `analytics` (the store and its query layer).

### Module layout

```
infra/modules/analytics/        # stateful: the durable store + the query surface
  terraform.tf
  variables.tf                  # bucket_name, project_name, results_prefix, environment,
                                #   force_destroy, lifecycle + Athena tuning knobs
  locals.tf                     # glue_database_name, athena_workgroup_name, athena_results_bucket
  storage.tf                    # extractions bucket + access-log sibling  (was results storage.tf)
  athena.tf                     # athena-results bucket, Glue db + table, workgroup  (unchanged)
  outputs.tf                    # bucket_name, bucket_arn, glue_database_name,
                                #   glue_table_name, athena_workgroup_name, ...

infra/modules/publisher/        # stateless: the Streams -> S3 feed
  terraform.tf
  variables.tf                  # function_name, source_table_stream_arn,
                                #   analytics_bucket_name, analytics_bucket_arn,
                                #   results_prefix, alarm_topic_arn, log_retention_days,
                                #   environment, + the function/stream tuning knobs (unchanged)
  locals.tf                     # source_file -> ../../../src/publisher/publisher.py, build_path
  publisher.tf                  # archive, log group, role + policy, function, ESM, alarms
  dlq.tf                        # DLQ + policy + DLQ alarm
  outputs.tf                    # function_name, function_arn, dlq_arn, dlq_name, ...
```

The Lambda source moves from `src/results/publisher.py` to `src/publisher/publisher.py`. The Lambda `handler = "publisher.handler"` is keyed off the *file* name, not the directory, so it is unchanged; only `locals.source_file` and the `src/results/**` path triggers in the two deploy workflows (plus the prod plan artifact path `infra/modules/results/.build/publisher.zip`) follow the rename.

The resource bodies move verbatim. No `aws_*` resource argument changes except the two cross-module references below.

### The interface between them

The dependency is one-directional: `publisher → analytics`. The publisher needs exactly two values from the store, and the store needs nothing from the publisher at runtime (the query layer simply reads the same prefix):

| `publisher.tf` reference today | becomes |
|---|---|
| `aws_s3_bucket.results.arn` (the `ResultsWrite` IAM scope) | `var.analytics_bucket_arn` |
| `aws_s3_bucket.results.bucket` (the `ANALYTICS_BUCKET_NAME` env var) | `var.analytics_bucket_name` |

That is the whole interface: two strings. The DLQ stays wholly inside `publisher`; the stream ARN still comes from `module.table`; the alarm topic still comes from `module.alarms`.

### Single-sourcing the partition prefix

The split is the occasion to fix the very lockstep risk ADR-0012 cited. The prefix `extractions/` is hoisted to a root local and threaded into both modules as `results_prefix`, collapsing the three independent literals to one source of truth:

- `analytics` uses it in the Glue `storage.location` and `storage.location.template`.
- `publisher` uses it in the `s3:PutObject` IAM scope (`${var.analytics_bucket_arn}/${var.results_prefix}/*`) and passes it to the Lambda as a `RESULTS_PREFIX` env var; `compose_key()` reads the env var instead of hardcoding the literal.

After this change the partition prefix is defined once at the root and consumed everywhere—the contract is more unified *after* the split than it was inside the bundled module.

### The payload ↔ catalog coupling

The other coupling—the publisher's payload fields versus the Glue table's column list—cannot be collapsed to a single literal (a Python dict on one side, an HCL column block on the other), and the split does not change that. It already spanned the Python/HCL boundary inside the bundled module; it now also spans a directory boundary. It stays a documented lockstep, guarded exactly as before: the integration smoke test asserts the round-trip object lands and matches, and the Glue SerDe's `ignore.malformed.json` tolerates additive drift. ADR-0012's post-implementation note on this coupling remains the reference.

### Naming

`publisher` and `analytics` are not new names—they are the names the function, the outputs, and the README already use. `publisher` is the active, disposable feed; `analytics` is the durable subsystem it feeds (store + query). We deliberately do **not** name the combined concern "publisher": that would name the smallest and most replaceable of the three parts while hiding the durable store, which is the part that actually matters.

The Glue database and Athena workgroup are renamed too—`…_results` → `…_analytics` and `…-results` → `…-analytics`—so the overload is resolved in the data layer as well, not just at the module wrapper. A Glue database is a namespace and an Athena workgroup is a query-routing/cost boundary; neither names a dataset—the *table* does, and it stays `extractions`, the same word the analytics bucket (`…-extractions-…`) and the partition prefix (`extractions/`) already use. Naming the namespace and the workgroup after the subsystem they belong to—`analytics`—reads naturally at the query (`<project>_<env>_analytics.extractions`: "the `extractions` table in the `analytics` database") and matches the module they live in. After this, nothing in the query layer carries the word `results`, so `…-results` denotes exactly one thing: the DynamoDB table, the system of record (ADR-0007).

### Root wiring

```hcl
locals {
  results_prefix = "extractions"
}

module "analytics" {
  source         = "./modules/analytics"
  bucket_name    = local.results_bucket_name
  project_name   = var.project_name
  results_prefix = local.results_prefix
  force_destroy  = var.environment != "prod"
  environment    = var.environment
}

module "publisher" {
  source                  = "./modules/publisher"
  function_name           = "${var.project_name}-${var.environment}-publisher"
  source_table_stream_arn = module.table.stream_arn
  analytics_bucket_name   = module.analytics.bucket_name
  analytics_bucket_arn    = module.analytics.bucket_arn
  results_prefix          = local.results_prefix
  log_retention_days      = var.environment == "prod" ? 30 : 14
  environment             = var.environment
  alarm_topic_arn         = module.alarms.topic_arn
}
```

The root outputs re-point from `module.results.*` to `module.analytics.*` / `module.publisher.*`. The output *keys* already read `analytics_bucket_*` and `publisher_*`; the two that still said `results_` (`results_glue_database_name`, `results_athena_workgroup_name`) are renamed to `analytics_glue_database_name` / `analytics_athena_workgroup_name`, so every output of the analytics module now shares the `analytics_` prefix—consistent with the Glue/workgroup naming decision above.

### Migration (no state surgery pre-prod)

Because the module is not deployed to any protected environment—there is no prod, and staging is `force_destroy`-enabled—the split is a straight refactor. The resources are recreated under their new module addresses (`module.results.*` → `module.analytics.*` / `module.publisher.*`); no `moved {}` blocks or `terraform state mv` are required, and a destroy/recreate of the (empty) buckets and catalog objects is acceptable. The Glue database and Athena workgroup renames are themselves force-new changes, so they ride along on this same recreate and add no migration step.

Were this already applied to a protected environment, the safe path would instead be `moved {}` blocks mapping each old address to its new one, making the split a state-only no-op with an empty `plan`. That path is recorded here only so the option is on the record; it is not the path taken.

## Consequences

Positive:

- The infrastructure is once again all per-concern modules—the README's stated organizing principle holds with no exception.
- The module names match the names the code, outputs, and docs already use—and with the Glue database and Athena workgroup renamed to `…_analytics`/`…-analytics`, nothing in the query layer carries the word either. "results" now unambiguously means the DynamoDB table, with no resource of any kind one separator away from it.
- Stateful storage is isolated from disposable compute. The bucket holding the durable record no longer shares a module—or a plan blast radius—with a churny Lambda. The publisher can be destroyed and recreated without ever touching the store.
- The partition prefix is single-sourced at the root (one `local`, one Lambda env var) instead of triplicated, directly resolving the lockstep concern ADR-0012 bundled to avoid.

Negative:

- The root gains a second module block, a shared `local`, and the two-string wiring between them—marginally more root surface than one module.
- The payload ↔ Glue-column coupling now also crosses a directory boundary (it already crossed the Python/HCL boundary). Mitigation is unchanged: the smoke test asserts the round-trip, and the SerDe tolerates additive drift.
- ADR-0012's "one module, one partition contract" framing is retired. The contract is still single-sourced—better than before for the prefix—but now via a root local rather than module co-location.

Neutral:

- No behavioral change. Same Lambda code, same event source mapping levers, same bucket hardening and lifecycle, same Glue projection and Athena scan cap, same IAM and alarms. In a fresh environment the resulting infrastructure matches what ADR-0012 produced in every resource configuration and behavior; the only differences are the packaging (two modules instead of one) and the two renamed strings—the Glue database and Athena workgroup (next bullet).
- Renaming the Glue database and Athena workgroup is a metadata-only recreate. `name` is force-new on both, so `terraform apply` replaces them—and the `extractions` table that parents to the database—under the new names. No S3 data is touched: the `extractions/…` objects and the bucket name are unchanged, and partition projection means there are no materialized partitions to lose. The `extractions` table name and its schema are unchanged; only the database and workgroup strings move.

## Alternatives considered

- **Keep the bundle (ADR-0012 status quo).** Rejected. The co-location benefit was always notional—the contract spanned Python and HCL regardless—and it is now outweighed by the naming overload, the per-concern principle, and the stateful/stateless lifecycle mismatch. Pre-prod is the cheapest moment to act.
- **Rename only, `results` → `analytics`.** Viable and low-cost; it would fix the name collision with the table without restructuring. Rejected in favor of the full split because it leaves the durable store and the disposable Lambda in one module and the per-concern principle still violated—and with no downtime cost pre-prod, there is nothing to defer for.
- **Rename only, `results` → `publisher`.** Rejected. It names the smallest, most replaceable part and hides the durable analytics store, which is the opposite of what the name should foreground.
- **Three modules—`publisher` + `bucket` + `query`.** Rejected, consistent with ADR-0012's own reasoning. The Glue table's `storage.location` points *into* the bucket; storage and query share a lifecycle and change together. Splitting them would put `storage.location` across a module edge for no lifecycle gain. Two is the right cut.
- **Data layer: keep `results`, or rename it to `extractions`.** The Glue database and workgroup were renamed to `analytics` over two other choices. *Keeping `results`* (an earlier draft of this ADR) was rejected: it leaves the workgroup name byte-for-byte identical to the DynamoDB table (`<project>-<env>-results`) and the database one separator away (`<project>_<env>_results`), so the overload the split removes at the module layer survives untouched in the data layer—and the "`results` now means one thing" win the split claims never actually lands. *Renaming to `extractions`* (matching the bucket, prefix, and table) is internally consistent but reads redundantly as `…_extractions.extractions`; a database is a namespace and is better named for the subsystem (`analytics`) than for the one dataset it currently holds, while the *table* keeps the dataset name (`extractions`). `analytics` also matches the module the resources live in, so the data-layer and module-layer names finally agree.

## Relationship to ADR-0012

This ADR supersedes ADR-0012's **packaging and naming** decision only. Every resource-level decision ADR-0012 made stands and carries forward unchanged: the Streams-consumer design and its batch/retry/bisect levers, the natural idempotency of the `{document_id}.json` key, the four-layer bucket hardening, `STANDARD`-tier-only with no current-version expiration, partition projection over a crawler or `MSCK REPAIR`, JSON over Parquet, the dedicated workgroup with its per-query scan cap, the prefix-scoped IAM, and the function- and DLQ-level alarms. ADR-0012—including its post-implementation notes—remains the reference for *why each resource is built the way it is*; this ADR governs *where those resources live and what the modules—and the Glue database and Athena workgroup—are called*.
