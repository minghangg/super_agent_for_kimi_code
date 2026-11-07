"""Super-agent engine skeleton (M0: read-only, observe-first).

Every module in this package is deterministic and holds no credentials.
External side effects are centralised behind ``Engine.dispatch_enabled`` and
counted, so the default observe mode can be verified to perform zero of them.
"""

__version__ = "0.1.0"
