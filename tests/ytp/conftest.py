"""Shared fixtures for the ytp suite."""

from __future__ import annotations

import shutil

import pytest


@pytest.fixture
def rules_root(tmp_path, monkeypatch):
    """Copy series rules into a temp repo so tests can append freely."""
    root = tmp_path / "repo"
    source = root / "rules"
    source.mkdir(parents=True)
    real = _repo_rules()
    for path in real.glob("*.yaml"):
        shutil.copy(path, source / path.name)
    return root


def _repo_rules():
    from ytp.rules import repo_root

    return repo_root() / "rules"
