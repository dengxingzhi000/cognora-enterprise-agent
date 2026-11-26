"""Compatibility import for the LangGraph-native expense workflow."""
from .graph import build_expense_graph, run_expense_workflow

__all__ = ["build_expense_graph", "run_expense_workflow"]
