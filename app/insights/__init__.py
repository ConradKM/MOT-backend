"""Insights: a business's own analytics page (``/api/insights``).

Read-only, computed on request from the rows the product already writes - no
rollup tables, no jobs. Shares its appointment/booking-request definitions
with Platform Admin's tenant statistics via app/analytics/queries.py.
"""
