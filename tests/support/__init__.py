"""Helpers shared by several test modules (``from support.<topic> import ...``).

Test modules never import each other: what more than one of them needs lives here, so each
module can be renamed, split or run alone (``pytest tests/test_x.py``, ``pytest -n auto``).
"""
