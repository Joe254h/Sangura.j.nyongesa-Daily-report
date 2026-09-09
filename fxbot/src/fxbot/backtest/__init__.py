"""Backtrader adapter and research tooling.

Backtrader is deliberately chosen and deliberately quarantined: nothing outside this
package may import it (§17.15). If it is ever replaced, only this package changes.

``costs.py`` is the one exception to the dependency direction: ``execution/paper_broker.py``
imports it, because both engines must share exactly one fill model or ``test_parity.py``
can never pass (§12.5). ``tests/test_layering.py`` asserts that edge as an allow-listed
exception rather than ignoring it.
"""
