locals {
  # Glue databases conventionally use underscores so Athena does not need
  # backtick-quoting; the workgroup follows the pipeline-wide hyphen naming.
  glue_database_name    = "${var.project_name}_${var.environment}_analytics"
  athena_workgroup_name = "${var.project_name}-${var.environment}-analytics"
  athena_results_bucket = "${substr(var.bucket_name, 0, min(length(var.bucket_name), 48))}-athena-results"
}
