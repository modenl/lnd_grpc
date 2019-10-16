import grpc
from grpc._channel import _Rendezvous

from lnd_grpc.base_client import BaseClient
from lnd_grpc.invoices import Invoices
from lnd_grpc.lightning import Lightning
from lnd_grpc.object_proxy import ObjectProxy
from lnd_grpc.wallet_unlocker import WalletUnlocker
from lnd_grpc.config import defaultNetwork, defaultRPCHost, defaultRPCPort


class Client(Lightning, WalletUnlocker, Invoices):
    def __init__(
            self,
            lnd_dir: str = None,
            macaroon_path: str = None,
            tls_cert_path: str = None,
            network: str = defaultNetwork,
            grpc_host: str = defaultRPCHost,
            grpc_port: str = defaultRPCPort,
    ):
        super().__init__(
            lnd_dir=lnd_dir,
            macaroon_path=macaroon_path,
            tls_cert_path=tls_cert_path,
            network=network,
            grpc_host=grpc_host,
            grpc_port=grpc_port,
        )


def retry(target, target_callable, *args, **kwargs):
    def can_retry(e):
        return hasattr(e, "code") and e.code() == grpc.StatusCode.UNAVAILABLE

    try:
        return target_callable(*args, **kwargs)
    except _Rendezvous as e:
        if not can_retry(e):
            raise
        target.connection_status_change = True
        if target.channel is not None:
            # Will leak if channel is not closed
            target.channel.close()

        return target_callable(*args, **kwargs)


def get_persistent_client(client):
    return ObjectProxy(client, invoker=retry)


__all__ = ["BaseClient", "WalletUnlocker", "Lightning", "Invoices", "Client",
           "get_persistent_client"]
