#!/usr/bin/env python3
"""Compatibility shim: the token CLI now lives in ``hansard_gateway.manage_tokens``.

The module moved into the package so the ``hg-tokens`` console script resolves
in a fresh clone / container (27.2-REQ-07). Direct ``python manage_tokens.py …``
invocations and the live runbook keep working through this shim.
"""

from hansard_gateway.manage_tokens import main

if __name__ == "__main__":
    raise SystemExit(main())
