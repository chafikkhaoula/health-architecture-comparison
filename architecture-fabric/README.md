# Fabric architecture

This package implements Architecture B from the experimental protocol:

- one isolated Fabric v2.5.15 network;
- two organizations, with one peer in each organization;
- one Raft orderer;
- chaincode endorsement policy
  `AND('Org1MSP.peer','Org2MSP.peer')`;
- PostgreSQL containing clinical payloads only;
- Fabric world state containing record hashes, non-clinical record
  locators, authorization rules, and audit events;
- one persistent Go Gateway bridge that waits for a confirmed `VALID`
  commit before returning a successful write;
- one Python adapter matching the shared `OP1`-`OP6` contract.

The network uses project-specific hostnames, container names, volume
names, network name, and host ports. It does not reuse the EAL or
PrivHealth Fabric network.

## Port allocation

| Component | Host port |
|---|---:|
| Orderer client endpoint | 27050 |
| Orderer admin endpoint | 27053 |
| Orderer operations endpoint | 29443 |
| Org1 peer / Gateway endpoint | 27051 |
| Org1 operations endpoint | 29444 |
| Org2 peer endpoint | 29051 |
| Org2 operations endpoint | 29445 |
| Go Gateway bridge | 18081 |
| Off-chain PostgreSQL | 55433 |

All published endpoints bind only to localhost.

## Prerequisites

- Docker Engine and Docker Compose;
- Go 1.25 or a Go installation supporting automatic toolchain
  selection;
- Fabric v2.5.15 binaries (`cryptogen`, `configtxgen`, `peer`, and
  `osnadmin`);
- the pinned Fabric v2.5.15 container images;
- Python 3.12 and the root Python dependencies.

The default binary and config paths match:

```text
/home/khcha/projects/fabric-samples/bin
/home/khcha/projects/fabric-samples/config
```

Both paths can be changed in `.env`.

## Clean deployment

From the repository root:

```bash
cp architecture-fabric/.env.example architecture-fabric/.env
```

Replace both PostgreSQL passwords in `.env`, then run:

```bash
architecture-fabric/scripts/deploy.sh
architecture-fabric/scripts/status.sh
```

`deploy.sh` performs these correctness-relevant steps:

1. verifies Fabric CLI versions;
2. creates project-specific MSP and TLS material;
3. creates and joins `healthchannel`;
4. packages and installs the vendored Go chaincode;
5. obtains Org1 and Org2 approvals for the same definition;
6. commits the explicit two-organization endorsement policy;
7. builds and starts the persistent Gateway bridge;
8. waits until the bridge health endpoint responds.

Generated MSP material, channel artifacts, chaincode package, Gateway
binary, private keys, and `.env` are ignored by Git.

## Python adapter

```python
from app.adapter import FabricAdapter

adapter = FabricAdapter(
    postgres_conninfo=(
        "host=127.0.0.1 port=55433 "
        "dbname=health_arch_fabric "
        "user=health_arch_fabric_app "
        "password=..."
    ),
    gateway_url="http://127.0.0.1:18081",
)
```

Use the adapter as an asynchronous context manager. Its
`last_transaction_receipt` property uses task-local context and exposes
the latest write transaction identifier, block number, and commit
validation status without changing the frozen response schemas.

## Validation commands

Python unit and contract tests:

```bash
.venv/bin/python -m pytest -q
```

Go chaincode and Gateway tests:

```bash
(
  cd architecture-fabric/chaincode
  go test ./...
  go vet ./...
)

(
  cd architecture-fabric/gateway
  go test ./...
  go vet ./...
)
```

Deployed Fabric integration tests:

```bash
architecture-fabric/scripts/run-integration-tests.sh
```

Controlled incomplete-endorsement check:

```bash
architecture-fabric/scripts/insufficient-endorsement.sh
```

The last command restricts endorsement to `Org1MSP`, records the
Gateway response and transaction identifier when available, then
queries the protected key to verify that no record evidence exists.

## Reset

```bash
architecture-fabric/scripts/reset.sh
```

The reset script removes only the `health-arch-fabric` containers and
volumes plus the two generated directories under
`architecture-fabric/network/`. It does not touch EAL, PrivHealth, or
`fabric-samples`.

## Storage and transaction boundary

OP1 inserts the payload inside an open PostgreSQL transaction and
submits the Fabric evidence transaction before allowing that database
transaction to commit. A Fabric failure therefore rolls the staged
payload insertion back.

PostgreSQL and Fabric do not provide a shared distributed transaction.
A rare PostgreSQL commit failure after a `VALID` Fabric commit cannot be
rolled back on the ledger. The adapter preserves the Fabric receipt in
task-local state so that the inconsistency can be logged and classified
instead of silently reported as success.

Fabric audit sequences are scoped per clinical record. This preserves
the strict ordering required by OP6 while avoiding an artificial global
world-state counter that would create cross-record MVCC contention.
