"""Incident copilot: a Nemotron-powered investigator built on PostmortemEnv.

The environment in ``engine/`` is the evidence store and the deterministic
grader. This package is the product layer on top of it: an agent that
investigates an incident blind (no oracle feedback), routes each step to the
cheapest Nemotron model that can do it, and writes the postmortem.
"""
