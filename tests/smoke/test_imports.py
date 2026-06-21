"""Smoke test: verify all runtime dependencies can be imported."""

import importlib


def test_import_python_ulid():
    importlib.import_module("ulid")


def test_import_msgpack():
    importlib.import_module("msgpack")


def test_import_pydantic():
    importlib.import_module("pydantic")


def test_import_pydantic_settings():
    importlib.import_module("pydantic_settings")


def test_import_aiosqlite():
    importlib.import_module("aiosqlite")


def test_import_sqlite_vec():
    importlib.import_module("sqlite_vec")


def test_import_cachebox():
    importlib.import_module("cachebox")


def test_import_tiktoken():
    importlib.import_module("tiktoken")


def test_import_numpy():
    importlib.import_module("numpy")


def test_import_networkx():
    importlib.import_module("networkx")


def test_import_yaml():
    importlib.import_module("yaml")


def test_import_structlog():
    importlib.import_module("structlog")


def test_import_smilex_package():
    importlib.import_module("smilex")
