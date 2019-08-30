import time
from os import environ

import grpc

import lnd_grpc.protos.rpc_pb2 as ln
import lnd_grpc.protos.rpc_pb2_grpc as lnrpc
from lnd_grpc.base_client import BaseClient
from lnd_grpc.config import defaultNetwork, defaultRPCHost, defaultRPCPort

# tell gRPC which cypher suite to use
environ["GRPC_SSL_CIPHER_SUITES"] = (
    "ECDHE-RSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-SHA256:ECDHE-RSA-AES256-SHA384:"
    "ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-AES128-GCM-SHA256:"
    "ECDHE-ECDSA-AES128-SHA256:ECDHE-ECDSA-AES256-SHA384:ECDHE-ECDSA-AES256-GCM-SHA384"
)


class Lightning(BaseClient):
    """
    A class which interacts with the LND Lightning sub-system
    """

    def __init__(
        self,
        lnd_dir: str = None,
        macaroon_path: str = None,
        tls_cert_path: str = None,
        network: str = defaultNetwork,
        grpc_host: str = defaultRPCHost,
        grpc_port: str = defaultRPCPort,
    ):

        self._lightning_stub: lnrpc.LightningStub = None
        self.version = None
        super().__init__(
            lnd_dir=lnd_dir,
            macaroon_path=macaroon_path,
            tls_cert_path=tls_cert_path,
            network=network,
            grpc_host=grpc_host,
            grpc_port=grpc_port,
        )

    @property
    def version(self):
        """
        :return: version of LND running
        """
        if self._version:
            return self._version
        self._version = self.get_info().version.split(" ")[0]
        return self._version

    @version.setter
    def version(self, version: str):
        self._version = version

    @staticmethod
    def pack_into_channelbackups(single_backup):
        """
        This function will accept either an ln.ChannelBackup object as generated by
        export_chan_backup() or should be passed a single channel backup from
        export_all_channel_backups().single_chan_backups[index].

        It will then return a single channel backup packed into a ChannelBackups
        format as required by verify_chan_backup()
        """
        return ln.ChannelBackups(chan_backups=[single_backup])

    @property
    def lightning_stub(self) -> lnrpc.LightningStub:
        """
        Create the lightning stub used to interface with the Lightning sub-system.

        Connectivity to LND is monitored using a callback to the channel and if
        connection status changes the stub will be dynamically regenerated on next call.

        This helps to overcome issues where a sub-system is not active when the stub is
        created (e.g. calling Lightning sub-system when wallet not yet unlocked) which
        otherwise requires manual monitoring and regeneration
        """

        # if the stub is already created and channel might recover, return current stub
        if self._lightning_stub is not None and self.connection_status_change is False:
            return self._lightning_stub

        # otherwise, start by creating a fresh channel
        self.channel = grpc.secure_channel(
            target=self.grpc_address,
            credentials=self.combined_credentials,
            options=self.grpc_options,
        )

        # subscribe to channel connectivity updates with callback
        self.channel.subscribe(self.connectivity_event_logger)

        # create the new stub
        self._lightning_stub = lnrpc.LightningStub(self.channel)

        # 'None' is channel_status's initialization state.
        # ensure connection_status_change is True to keep regenerating fresh stubs until
        # channel comes online
        if self.connection_status is None:
            self.connection_status_change = True
            return self._lightning_stub
        self.connection_status_change = False
        return self._lightning_stub

    def wallet_balance(self):
        """
        Get (bitcoin) wallet balance, not in channels

        :return: WalletBalanceResponse with 3 attributes: 'total_balance',
        'confirmed_balance', 'unconfirmed_balance'
        """
        request = ln.WalletBalanceRequest()
        response = self.lightning_stub.WalletBalance(request)
        return response

    def channel_balance(self):
        """
        Get total channel balance and pending channel balance

        :return: ChannelBalanceResponse with 2 attributes: 'balance' and
        'pending_open_balance'
        """
        request = ln.ChannelBalanceRequest()
        response = self.lightning_stub.ChannelBalance(request)
        return response

    def get_transactions(self):
        """
        Describe all the known transactions relevant to the wallet

        :returns: TransactionDetails with 1 attribute: 'transactions', containing a list
        of all transactions
        """
        request = ln.GetTransactionsRequest()
        response = self.lightning_stub.GetTransactions(request)
        return response

    # TODO: add estimate_fee

    # On Chain
    def send_coins(self, addr: str, amount: int = None, **kwargs):
        """
        Allows sending coins to a single output
        If neither target_conf or sat_per_byte are set, wallet will use internal fee
        model

        :return: SendCoinsResponse with 1 attribute: 'txid'
        """
        request = ln.SendCoinsRequest(addr=addr, amount=amount, **kwargs)
        response = self.lightning_stub.SendCoins(request)
        return response

    def list_unspent(self, min_confs: int, max_confs: int):
        """
        Lists unspent UTXOs controlled by the wallet between the chosen confirmations

        :return: ListUnspentResponse with 1 attribute: 'utxo', which itself contains a
        list of utxos
        """
        request = ln.ListUnspentRequest(min_confs=min_confs, max_confs=max_confs)
        response = self.lightning_stub.ListUnspent(request)
        return response

    # Response-streaming RPC
    def subscribe_transactions(self):
        """
        Creates a uni-directional stream from the server to the client in which any
        newly discovered transactions relevant to the wallet are sent over

        :return: iterable of Transactions with 8 attributes per response. See the notes
        on threading and iterables in README.md
        """
        request = ln.GetTransactionsRequest()
        return self.lightning_stub.SubscribeTransactions(request)

    def send_many(self, addr_to_amount: ln.SendManyRequest.AddrToAmountEntry, **kwargs):
        """
        Send a single transaction involving multiple outputs

        :return: SendManyResponse with 1 attribute: 'txid'
        """
        request = ln.SendManyRequest(AddrToAmount=addr_to_amount, **kwargs)
        response = self.lightning_stub.SendMany(request)
        return response

    def new_address(self, address_type: str):
        """
        Create a new wallet address of either p2wkh or np2wkh type.

        :return: NewAddressResponse with 1 attribute: 'address'
        """
        if address_type == "p2wkh":
            request = ln.NewAddressRequest(type="WITNESS_PUBKEY_HASH")
        elif address_type == "np2wkh":
            request = ln.NewAddressRequest(type="NESTED_PUBKEY_HASH")
        else:
            return TypeError(
                "invalid address type %s, supported address type are: p2wkh and np2wkh"
                % address_type
            )
        response = self.lightning_stub.NewAddress(request)
        return response

    def sign_message(self, msg: str):
        """
        Returns the signature of the message signed with this node’s private key.
        The returned signature string is zbase32 encoded and pubkey recoverable, meaning
        that only the message digest and signature are needed for verification.

        :return: SignMessageResponse with 1 attribute: 'signature'
        """
        _msg_bytes = msg.encode("utf-8")
        request = ln.SignMessageRequest(msg=_msg_bytes)
        response = self.lightning_stub.SignMessage(request)
        return response

    def verify_message(self, msg: str, signature: str):
        """
        Verifies a signature over a msg. The signature must be zbase32 encoded and
        signed by an active node in the resident node’s channel database. In addition to
        returning the validity of the signature, VerifyMessage also returns the
        recovered pubkey from the signature.

        :return: VerifyMessageResponse with 2 attributes: 'valid' and 'pubkey'
        """
        _msg_bytes = msg.encode("utf-8")
        request = ln.VerifyMessageRequest(msg=_msg_bytes, signature=signature)
        response = self.lightning_stub.VerifyMessage(request)
        return response

    def connect_peer(
        self, addr: ln.LightningAddress, perm: bool = 0, timeout: int = None
    ):
        """
        Attempts to establish a connection to a remote peer. This is at the networking
        level, and is used for communication between nodes. This is distinct from
        establishing a channel with a peer.

        :return: ConnectPeerResponse with no attributes
        """
        request = ln.ConnectPeerRequest(addr=addr, perm=perm)
        response = self.lightning_stub.ConnectPeer(request, timeout=timeout)
        return response

    def connect(self, address: str, perm: bool = 0, timeout: int = None):
        """
        Custom function which allows passing address in a more natural
        "pubkey@127.0.0.1:9735" string format into connect_peer()

        :return: ConnectPeerResponse with no attributes
        """
        pubkey, host = address.split("@")
        _address = self.lightning_address(pubkey=pubkey, host=host)
        response = self.connect_peer(addr=_address, perm=perm, timeout=timeout)
        return response

    def disconnect_peer(self, pub_key: str):
        """
        attempts to disconnect one peer from another identified by a given pubKey.
        In the case that we currently have a pending or active channel with the target
        peer, then this action will be not be allowed.

        :return: DisconnectPeerResponse with no attributes
        """
        request = ln.DisconnectPeerRequest(pub_key=pub_key)
        response = self.lightning_stub.DisconnectPeer(request)
        return response

    def list_peers(self):
        """
        returns a verbose listing of all currently active peers

        :return: ListPeersResponse.peers with no attributes
        """
        request = ln.ListPeersRequest()
        response = self.lightning_stub.ListPeers(request)
        return response.peers

    def get_info(self):
        """
        returns general information concerning the lightning node including it’s
        identity pubkey, alias, the chains it is connected to, and information
        concerning the number of open+pending channels.

        :return: GetInfoResponse with 14 attributes
        """
        request = ln.GetInfoRequest()
        response = self.lightning_stub.GetInfo(request)
        return response

    def pending_channels(self):
        """
        returns a list of all the channels that are currently considered “pending”.
        A channel is pending if it has finished the funding workflow and is waiting for
        confirmations for the funding txn, or is in the process of closure, either
        initiated cooperatively or non-cooperatively

        :return: PendingChannelsResponse with 5 attributes: 'total_limbo_balance',
        'pending_open_channels', 'pending_closing_channels',
        'pending_force_closing_channels' and 'waiting_close_channels'
        """
        request = ln.PendingChannelsRequest()
        response = self.lightning_stub.PendingChannels(request)
        return response

    def list_channels(self, **kwargs):
        """
        returns a description of all the open channels that this node is a participant
        in.

        :return: ListChannelsResponse with 1 attribute: 'channels' that contains a list
        of the channels queried
        """
        request = ln.ListChannelsRequest(**kwargs)
        response = self.lightning_stub.ListChannels(request)
        return response.channels

    def closed_channels(self, **kwargs):
        """
        returns a description of all the closed channels that this node was a
        participant in.

        :return: ClosedChannelsResponse with 1 attribute: 'channels'
        """
        request = ln.ClosedChannelsRequest(**kwargs)
        response = self.lightning_stub.ClosedChannels(request)
        return response.channels

    def open_channel_sync(self, local_funding_amount: int, **kwargs):
        """
        synchronous version of the OpenChannel RPC call. This call is meant to be
        consumed by clients to the REST proxy. As with all other sync calls, all byte
        slices are intended to be populated as hex encoded strings.

        :return: ChannelPoint with 3 attributes: 'funding_txid_bytes', 'funding_tx_str'
        and 'output_index'
        """
        request = ln.OpenChannelRequest(
            local_funding_amount=local_funding_amount, **kwargs
        )
        response = self.lightning_stub.OpenChannelSync(request)
        return response

    # Response-streaming RPC
    def open_channel(self, local_funding_amount: int, timeout: int = None, **kwargs):
        """
        attempts to open a singly funded channel specified in the request to a remote
        peer. Users are able to specify a target number of blocks that the funding
        transaction should be confirmed in, or a manual fee rate to us for the funding
        transaction. If neither are specified, then a lax block confirmation target is
        used.

        :return: an iterable of OpenChannelStatusUpdates. See the notes on threading and
        iterables in README.md
        """
        # TODO: implement `lncli openchannel --connect` function
        request = ln.OpenChannelRequest(
            local_funding_amount=local_funding_amount, **kwargs
        )
        if request.node_pubkey == b"":
            request.node_pubkey = bytes.fromhex(request.node_pubkey_string)
        return self.lightning_stub.OpenChannel(request, timeout=timeout)

    # Response-streaming RPC
    def close_channel(self, channel_point, **kwargs):
        """
        attempts to close an active channel identified by its channel outpoint
        (ChannelPoint). The actions of this method can additionally be augmented to
        attempt a force close after a timeout period in the case of an inactive peer.
        If a non-force close (cooperative closure) is requested, then the user can
        specify either a target number of blocks until the closure transaction is
        confirmed, or a manual fee rate. If neither are specified, then a default
        lax, block confirmation target is used.

        :return: an iterable of CloseChannelStatusUpdates with 2 attributes per
        response. See the notes on threading and iterables in README.md
        """
        funding_txid, output_index = channel_point.split(":")
        _channel_point = self.channel_point_generator(
            funding_txid=funding_txid, output_index=output_index
        )
        request = ln.CloseChannelRequest(channel_point=_channel_point, **kwargs)
        return self.lightning_stub.CloseChannel(request)

    def close_all_channels(self, inactive_only: bool = 0):
        """
        Custom function which iterates over all channels and closes them sequentially
        using close_channel()

        :return: CloseChannelStatusUpdate for each channel close, with 2 attributes:
        'close_pending' and 'chan_close'
        """
        if not inactive_only:
            for channel in self.list_channels():
                self.close_channel(channel_point=channel.channel_point).next()
        if inactive_only:
            for channel in self.list_channels(inactive_only=1):
                self.close_channel(channel_point=channel.channel_point).next()

    def abandon_channel(self, channel_point: ln.ChannelPoint):
        """
        removes all channel state from the database except for a close summary.
        This method can be used to get rid of permanently unusable channels due to bugs
        fixed in newer versions of lnd.
        Only available when in debug builds of lnd.

        :return: AbandonChannelResponse with no attributes
        """
        funding_txid, output_index = channel_point.split(":")
        _channel_point = self.channel_point_generator(
            funding_txid=funding_txid, output_index=output_index
        )
        request = ln.AbandonChannelRequest(channel_point=_channel_point)
        response = self.lightning_stub.AbandonChannel(request)
        return response

    @staticmethod
    def send_request_generator(**kwargs):
        """
        Creates the SendRequest object for the synchronous streaming send_payment() as a
        generator

        :return: generator object for the request
        """
        # Commented out to complement the magic sleep below...
        # while True:
        request = ln.SendRequest(**kwargs)
        yield request
        # Magic sleep which tricks the response to the send_payment() method to actually
        # contain data...
        time.sleep(5)

    # Bi-directional streaming RPC
    def send_payment(self, **kwargs):
        """
        dispatches a bi-directional streaming RPC for sending payments through the
        Lightning Network. A single RPC invocation creates a persistent bi-directional
        stream allowing clients to rapidly send payments through the Lightning Network
        with a single persistent connection.

        :return: an iterable of SendResponses with 4 attributes per response.
        See the notes on threading and iterables in README.md
        """
        # Use payment request as first choice
        if "payment_request" in kwargs:
            params = {"payment_request": kwargs["payment_request"]}
            if "amt" in kwargs:
                params["amt"] = kwargs["amt"]
            request_iterable = self.send_request_generator(**params)
        else:
            # Helper to convert hex to bytes automatically
            try:
                if "payment_hash" not in kwargs:
                    kwargs["payment_hash"] = bytes.fromhex(
                        kwargs["payment_hash_string"]
                    )
                if "dest" not in kwargs:
                    kwargs["dest"] = bytes.fromhex(kwargs["dest_string"])
            except ValueError as e:
                raise e
            request_iterable = self.send_request_generator(**kwargs)
        return self.lightning_stub.SendPayment(request_iterable)

    # Synchronous non-streaming RPC
    def send_payment_sync(self, **kwargs):
        """
         synchronous non-streaming version of SendPayment. This RPC is intended to be
         consumed by clients of the REST proxy. Additionally, this RPC expects the
         destination’s public key and the payment hash (if any) to be encoded as hex
         strings.

        :return: SendResponse with up to 4 attributes: 'payment_error' (conditional),
        'payment_preimage', 'payment_route' and 'payment_hash'
        """
        # Use payment request as first choice
        if "payment_request" in kwargs:
            params = {"payment_request": kwargs["payment_request"]}
            if "amt" in kwargs:
                params["amt"] = kwargs["amt"]
            request = ln.SendRequest(**params)
        else:
            request = ln.SendRequest(**kwargs)
        response = self.lightning_stub.SendPaymentSync(request)
        return response

    def pay_invoice(self, payment_request: str):
        """
        Custom function which only takes a payment request and pays the invoice using
        the asynchronous send_payment_sync()

        :return: SendResponse with up to 4 attributes: 'payment_error' (conditional),
        'payment_preimage', 'payment_route' and 'payment_hash'
        """
        response = self.send_payment_sync(payment_request=payment_request)
        return response

    @staticmethod
    def send_to_route_generator(invoice, route):
        """
        create SendToRouteRequest generator

        :return: generator of SendToRouteRequest
        """
        # Commented out to complement the magic sleep below...
        # while True:
        request = ln.SendToRouteRequest(payment_hash=invoice.r_hash, route=route)
        yield request
        # Magic sleep which tricks the response to the send_to_route() method to
        # actually contain data...
        time.sleep(5)

    # Bi-directional streaming RPC
    def send_to_route(self, invoice, route):
        """
        bi-directional streaming RPC for sending payment through the Lightning Network.
        This method differs from SendPayment in that it allows users to specify a full
        route manually.
        This can be used for things like rebalancing, and atomic swaps.

        :return: an iterable of SendResponses with 4 attributes per response.
        See the notes on threading and iterables in README.md
        """
        request_iterable = self.send_to_route_generator(invoice=invoice, route=route)
        return self.lightning_stub.SendToRoute(request_iterable)

    # Synchronous non-streaming RPC
    def send_to_route_sync(self, route, **kwargs):
        """
        a synchronous version of SendToRoute. It Will block until the payment either
        fails or succeeds.

        :return: SendResponse with up to 4 attributes: 'payment_error' (conditional),
        'payment_preimage', 'payment_route' and 'payment_hash'
        """
        request = ln.SendToRouteRequest(route=route, **kwargs)
        response = self.lightning_stub.SendToRouteSync(request)
        return response

    def add_invoice(
        self,
        memo: str = "",
        value: int = 0,
        expiry: int = 3600,
        creation_date: int = int(time.time()),
        **kwargs
    ):
        """
        attempts to add a new invoice to the invoice database. Any duplicated invoices
        are rejected, therefore all invoices must have a unique payment preimage.

        :return: AddInvoiceResponse with 3 attributes: 'r_hash', 'payment_request' and
        'add_index'
        """
        request = ln.Invoice(
            memo=memo, value=value, expiry=expiry, creation_date=creation_date, **kwargs
        )
        response = self.lightning_stub.AddInvoice(request)
        return response

    def list_invoices(self, reversed: bool = 1, **kwargs):
        """
        returns a list of all the invoices currently stored within the database.
        Any active debug invoices are ignored. It has full support for paginated
        responses, allowing users to query for specific invoices through their
        add_index. This can be done by using either the first_index_offset or
        last_index_offset fields included in the response as the index_offset of the
        next request. By default, the first 100 invoices created will be returned.
        Backwards pagination is also supported through the Reversed flag.

        :return: ListInvoiceResponse with 3 attributes: 'invoices' containing a list of
        queried invoices, 'last_index_offset' and 'first_index_offset'
        """
        request = ln.ListInvoiceRequest(reversed=reversed, **kwargs)
        response = self.lightning_stub.ListInvoices(request)
        return response

    def lookup_invoice(self, **kwargs):
        """
        attempts to look up an invoice according to its payment hash.
        The passed payment hash must be exactly 32 bytes, if not, an error is returned.

        :return: Invoice with 21 attributes
        """
        request = ln.PaymentHash(**kwargs)
        response = self.lightning_stub.LookupInvoice(request)
        return response

    def subscribe_invoices(self, **kwargs):
        """
        a uni-directional stream (server -> client) for notifying the client of newly
        added/settled invoices. The caller can optionally specify the add_index and/or
        the settle_index. If the add_index is specified, then we’ll first start by
        sending add invoice events for all invoices with an add_index greater than the
        specified value. If the settle_index is specified, the next, we’ll send out all
        settle events for invoices with a settle_index greater than the specified value.
        One or both of these fields can be set.
        If no fields are set, then we’ll only send out the latest add/settle events.

        :return: an iterable of Invoice objects with 21 attributes per response.
        See the notes on threading and iterables in README.md
        """
        request = ln.InvoiceSubscription(**kwargs)
        return self.lightning_stub.SubscribeInvoices(request)

    def decode_pay_req(self, pay_req: str):
        """
        takes an encoded payment request string and attempts to decode it, returning a
        full description of the conditions encoded within the payment request.

        :return: PayReq with 10 attributes
        """
        request = ln.PayReqString(pay_req=pay_req)
        response = self.lightning_stub.DecodePayReq(request)
        return response

    def list_payments(self):
        """
        returns a list of all outgoing payments

        :return: ListPaymentsResponse with 1 attribute: 'payments', containing a list
        of payments
        """
        request = ln.ListPaymentsRequest()
        response = self.lightning_stub.ListPayments(request)
        return response

    def delete_all_payments(self):
        """
        deletes all outgoing payments from DB.

        :return: DeleteAllPaymentsResponse with no attributes
        """
        request = ln.DeleteAllPaymentsRequest()
        response = self.lightning_stub.DeleteAllPayments(request)
        return response

    def describe_graph(self, **kwargs):
        """
        a description of the latest graph state from the point of view of the node.
        The graph information is partitioned into two components: all the
        nodes/vertexes, and all the edges that connect the vertexes themselves.
        As this is a directed graph, the edges also contain the node directional
        specific routing policy which includes: the time lock delta, fee information etc

        :return: ChannelGraph object with 2 attributes: 'nodes' and 'edges'
        """
        request = ln.ChannelGraphRequest(**kwargs)
        response = self.lightning_stub.DescribeGraph(request)
        return response

    def get_chan_info(self, chan_id: int):
        """
        the latest authenticated network announcement for the given channel identified
        by its channel ID: an 8-byte integer which uniquely identifies the location of
        transaction’s funding output within the blockchain.

        :return: ChannelEdge object with 8 attributes
        """
        request = ln.ChanInfoRequest(chan_id=chan_id)
        response = self.lightning_stub.GetChanInfo(request)
        return response

    # Uni-directional stream
    def subscribe_channel_events(self):
        """
        creates a uni-directional stream from the server to the client in which any
        updates relevant to the state of the channels are sent over. Events include new
        active channels, inactive channels, and closed channels.

        :return: an iterator of ChannelEventUpdate objects with 5 attributes per
        response. See the notes on threading and iterables in README.md
        """
        request = ln.ChannelEventSubscription()
        return self.lightning_stub.SubscribeChannelEvents(request)

    def get_node_info(self, pub_key: str):
        """
        returns the latest advertised, aggregated, and authenticated channel information
        for the specified node identified by its public key.

        :return: NodeInfo object with 3 attributes: 'node', 'num_channels' and
        'total_capacity'
        """

        request = ln.NodeInfoRequest(pub_key=pub_key)
        response = self.lightning_stub.GetNodeInfo(request)
        return response

    def query_routes(self, pub_key: str, amt: int, **kwargs):
        """
        attempts to query the daemon’s Channel Router for a possible route to a target
        destination capable of carrying a specific amount of satoshis.
        The returned route contains the full details required to craft and send an HTLC,
        also including the necessary information that should be present within the
        Sphinx packet encapsulated within the HTLC.

        :return: QueryRoutesResponse object with 1 attribute: 'routes' which contains a
        single route
        """
        request = ln.QueryRoutesRequest(pub_key=pub_key, amt=amt, **kwargs)
        response = self.lightning_stub.QueryRoutes(request)
        return response.routes

    def get_network_info(self):
        """
        returns some basic stats about the known channel graph from the point of view of
        the node.

        :return: NetworkInfo object with 10 attributes
        """
        request = ln.NetworkInfoRequest()
        response = self.lightning_stub.GetNetworkInfo(request)
        return response

    def stop_daemon(self):
        """
        will send a shutdown request to the interrupt handler, triggering a graceful
        shutdown of the daemon.

        :return: StopResponse with no attributes
        """
        request = ln.StopRequest()
        response = self.lightning_stub.StopDaemon(request)
        return response

    # Response-streaming RPC
    def subscribe_channel_graph(self):
        """
        launches a streaming RPC that allows the caller to receive notifications upon
        any changes to the channel graph topology from the point of view of the
        responding node.
        Events notified include: new nodes coming online, nodes updating their
        authenticated attributes, new channels being advertised, updates in the routing
        policy for a directional channel edge, and when channels are closed on-chain.

        :return: iterable of GraphTopologyUpdate with 3 attributes: 'node_updates',
        'channel_updates' and 'closed_chans'
        """
        request = ln.GraphTopologySubscription()
        return self.lightning_stub.SubscribeChannelGraph(request)

    def debug_level(self, **kwargs):
        """
        allows a caller to programmatically set the logging verbosity of lnd.
        The logging can be targeted according to a coarse daemon-wide logging level, or
        in a granular fashion to specify the logging for a target sub-system.

        Usage: client.debug_level(level_spec='debug')

        :return: DebugLevelResponse with 1 attribute: 'sub_systems'
        """
        request = ln.DebugLevelRequest(**kwargs)
        response = self.lightning_stub.DebugLevel(request)
        return response

    def fee_report(self):
        """
        allows the caller to obtain a report detailing the current fee schedule enforced
        by the node globally for each channel.

        :return: FeeReportResponse with 4 attributes: 'channel_fees', 'day_fee_sum',
        'week_fee_sum' and 'month_fee_sum'
        """
        request = ln.FeeReportRequest()
        response = self.lightning_stub.FeeReport(request)
        return response

    def update_channel_policy(
        self,
        chan_point: str,
        is_global: bool = False,
        base_fee_msat: int = 1000,
        fee_rate: float = 0.000001,
        time_lock_delta: int = 144,
    ):
        """
        allows the caller to update the fee schedule and channel policies for all
        channels globally, or a particular channel.

        :return: PolicyUpdateResponse with no attributes
        """
        if chan_point:
            funding_txid, output_index = chan_point.split(":")
            channel_point = self.channel_point_generator(
                funding_txid=funding_txid, output_index=output_index
            )
        else:
            channel_point = None

        request = ln.PolicyUpdateRequest(
            chan_point=channel_point,
            base_fee_msat=base_fee_msat,
            fee_rate=fee_rate,
            time_lock_delta=time_lock_delta,
        )
        if is_global:
            setattr(request, "global", is_global)
        response = self.lightning_stub.UpdateChannelPolicy(request)
        return response

    def forwarding_history(self, **kwargs):
        """
        allows the caller to query the htlcswitch for a record of all HTLCs forwarded
        within the target time range, and integer offset within that time range.
        If no time-range is specified, then the first chunk of the past 24 hrs of
        forwarding history are returned.
        A list of forwarding events are returned.
        The size of each forwarding event is 40 bytes, and the max message size able to
        be returned in gRPC is 4 MiB.
        As a result each message can only contain 50k entries.
        Each response has the index offset of the last entry.
        The index offset can be provided to the request to allow the caller to skip a
        series of records.

        :return: ForwardingHistoryResponse with 2 attributes: 'forwarding_events' and
        'last_index_offset'
        """
        request = ln.ForwardingHistoryRequest(**kwargs)
        response = self.lightning_stub.ForwardingHistory(request)
        return response

    """
    Static channel backup
    """

    def export_chan_backup(self, **kwargs):
        """
        attempts to return an encrypted static channel backup for the target channel
        identified by its channel point.
        The backup is encrypted with a key generated from the aezeed seed of the user.
        The returned backup can either be restored using the RestoreChannelBackup
        method once lnd is running, or via the InitWallet and UnlockWallet methods from
        the WalletUnlocker service.

        :return: ChannelBackup with 2 attributes: 'chan_point' and 'chan_backup'
        """
        request = ln.ExportChannelBackupRequest(**kwargs)
        response = self.lightning_stub.ExportChannelBackup(request)
        return response

    def export_all_channel_backups(self, **kwargs):
        """
        returns static channel backups for all existing channels known to lnd.
        A set of regular singular static channel backups for each channel are returned.
        Additionally, a multi-channel backup is returned as well, which contains a
        single encrypted blob containing the backups of each channel.

        :return: ChanBackupSnapshot with 2 attributes: 'single_chan_backups' and
        'multi_chan_backup'
        """
        request = ln.ChanBackupExportRequest(**kwargs)
        response = self.lightning_stub.ExportAllChannelBackups(request)
        return response

    def verify_chan_backup(self, **kwargs):
        """
        allows a caller to verify the integrity of a channel backup snapshot.
        This method will accept either a packed Single or a packed Multi.
        Specifying both will result in an error.

        For multi_backup: works as expected.

        For single_chan_backups:
        Needs to be passed a single channel backup (ChannelBackup) packed into a
        ChannelBackups to verify sucessfully.

        export_chan_backup() returns a ChannelBackup but it is not packed properly.
        export_all_channel_backups().single_chan_backups returns a ChannelBackups but as
        it contains more than one channel, verify_chan_backup() will also reject it.

        Use helper method pack_into_channelbackups() to pack individual ChannelBackup
        objects into the appropriate ChannelBackups objects for verification.

        :return: VerifyChanBackupResponse with no attributes
        """
        request = ln.ChanBackupSnapshot(**kwargs)
        response = self.lightning_stub.VerifyChanBackup(request)
        return response

    def restore_chan_backup(self, **kwargs):
        """
        accepts a set of singular channel backups, or a single encrypted multi-chan
        backup and attempts to recover any funds remaining within the channel.
        If we are able to unpack the backup, then the new channel will be shown under
        listchannels, as well as pending channels.

        :return: RestoreBackupResponse with no attributes
        """
        request = ln.RestoreChanBackupRequest(**kwargs)
        response = self.lightning_stub.RestoreChannelBackups(request)
        return response

    # Response-streaming RPC
    def subscribe_channel_backups(self, **kwargs):
        """
        allows a client to sub-subscribe to the most up to date information concerning
        the state of all channel backups. Each time a new channel is added, we return
        the new set of channels, along with a multi-chan backup containing the backup
        info for all channels.
        Each time a channel is closed, we send a new update, which contains new new chan
        backups, but the updated set of encrypted multi-chan backups with the closed
        channel(s) removed.

        :return: iterable of ChanBackupSnapshot responses, with 2 attributes per
        response: 'single_chan_backups' and 'multi_chan_backup'
        """
        request = ln.ChannelBackupSubscription(**kwargs)
        response = self.lightning_stub.SubscribeChannelBackups(request)
        return response
