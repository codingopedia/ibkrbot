Proposed commit message: "Add IBKR safety/runbook updates and commission backfill"

- Add commission backfill plumbing (exec_id column/index, commission polling) and tests.
- Extend smoke CLI for LMT/no-wait and flatten safety flows; persistence for orders/commissions.
- Harden DB migrations (idempotent exec_id/index) with regression tests and closeout scripts.
- Document paper IBKR runbook and end-of-day summary; verified pytest, sim run, CLI helps.
