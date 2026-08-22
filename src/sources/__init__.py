"""Adapters that replace a fixture with a live read.

Each module here exposes the same contract: return the `workloads` mapping
`src/driftd.py` consumes, in the shape of `platform/observed-state.yaml`. That
keeps the verdict layer -- detection, attribution, policy evaluation, the
proposed patch -- unchanged whether the input is a checked-in fixture or a live
control plane, which is the only arrangement that lets the fixture be honest.
"""
