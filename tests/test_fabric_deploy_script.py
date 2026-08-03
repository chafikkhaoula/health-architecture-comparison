from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = PROJECT_ROOT / "architecture-fabric" / "scripts" / "deploy.sh"


def test_orderer_admin_api_is_ready_before_channel_join() -> None:
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    wait_start = script.index('echo "Waiting for orderer and peers"')
    join_start = script.index('echo "Joining the orderer to $CHANNEL_NAME"')
    readiness_loop = script[wait_start:join_start]

    assert '"$OSNADMIN" channel list' in readiness_loop
    assert '--orderer-address "127.0.0.1:$ORDERER_ADMIN_PORT"' in readiness_loop
    assert '--ca-file "$ORDERER_TLS_CA"' in readiness_loop
    assert '--client-cert "$ORDERER_TLS_CERT"' in readiness_loop
    assert '--client-key "$ORDERER_TLS_KEY"' in readiness_loop
    assert 'orderer_ready=true' in readiness_loop
    assert '[ "$orderer_ready" = true ]' in readiness_loop
