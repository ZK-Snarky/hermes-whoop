"""Hermes discovers this root entry point after installing the repository."""
from .hermes_whoop.plugin import register

__all__ = ["register"]
