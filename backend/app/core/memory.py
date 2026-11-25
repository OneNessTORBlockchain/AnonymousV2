"""Backwards-compatible name for the durable Learning Board.

The original flat note table lived here.  It is now a structured, reviewable
knowledge base in :mod:`app.core.learning`; this module keeps the old import
path (``from .memory import KINDS, KnowledgeBase``) working for tools, the
orchestrator and any external caller.

See ``docs/LEARNING-BOARD.md`` for the schema, the retrieval workflow and the
review rules.
"""
from __future__ import annotations

from .learning import (
    CATEGORIES,
    CATEGORY_PURPOSE,
    KINDS,
    RESULTS,
    ROLES,
    STATUSES,
    KnowledgeBase,
    infer_categories,
    looks_like_instructions,
    sanitize_secrets,
    tokens,
    utc_now,
)

__all__ = [
    "CATEGORIES",
    "CATEGORY_PURPOSE",
    "KINDS",
    "KnowledgeBase",
    "RESULTS",
    "ROLES",
    "STATUSES",
    "infer_categories",
    "looks_like_instructions",
    "sanitize_secrets",
    "tokens",
    "utc_now",
]
