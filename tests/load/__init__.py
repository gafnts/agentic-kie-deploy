"""
Load-test harness for the deployed pipeline (ADR-0015).

Marked ``load`` and deselected by default. Runs against staging only, drives the
real front door, and spends real LLM money; invoke via ``make load``.
"""
