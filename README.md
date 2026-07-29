# health-architecture-comparison

Reproducible experimental implementation for comparing:

- a Traditional PostgreSQL architecture; and
- a two-organization Hyperledger Fabric architecture with off-chain
  PostgreSQL payload storage.

Both adapters implement the frozen `OP1`-`OP6` contract in `shared/`.
Only synthetic FHIR R4 resources are permitted. The Fabric ledger never
stores a clinical payload.

## Validation

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

The PostgreSQL and Fabric integration suites are opt-in because they
require their corresponding deployed infrastructure.

## Architecture-specific setup

- Traditional: `architecture-traditional/`
- Fabric: `architecture-fabric/README.md`
- Frozen experiment design: `docs/experimental_protocol.md`
