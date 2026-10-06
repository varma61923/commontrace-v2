"""CLI command adapters, registered by :mod:`commontrace.cli`.

Each ``*_cmd`` module owns argument registration and dispatch for one command.
Keep these module paths stable for installed CLI and plugin integrations; reusable
memory or service logic belongs outside this package.
"""
