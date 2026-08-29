"""Stable Library Item domain entry point.

The existing implementation remains in ``catalogue`` during the incremental split so
the database queries and API behavior do not change in one large rewrite.
"""

from ..catalogue.service import library_item_service

__all__ = ["library_item_service"]
