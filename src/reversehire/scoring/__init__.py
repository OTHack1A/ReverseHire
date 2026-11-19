"""Relevance, fit and confidence scores, with explanations (no LLM)."""

from .embedding import Embedder, create_embedder
from .assessment import assess

__all__ = ["Embedder", "create_embedder", "assess"]
