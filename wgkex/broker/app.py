#!/usr/bin/env python3
"""wgkex broker"""
import dataclasses
import json
import re
import socket
from typing import Any, Dict, List, Tuple

import paho.mqtt.client as mqtt_client
from flask import Flask, render_template, request, Response
from flask.app import Flask as Flask_app
from flask_mqtt import Mqtt

from waitress import serve
from wgkex.config import config
from wgkex.common import logger
from wgkex.common.utils import is_valid_domain
from wgkex.broker.metrics import WorkerMetricsCollection
from wgkex.common.mqtt import (
    CONNECTED_PEERS_METRIC,
    TOPIC_BROKER_STATUS,
    TOPIC_WORKER_STATUS,
    TOPIC_WORKER_WG_DATA,
)

WG_PUBKEY_PATTERN = re.compile(r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw480]=$")
_HOSTNAME = socket.gethostname()


@dataclasses.dataclass
class KeyExchange:
    """A key exchange message.

    Attributes:
        public_key: The public key for this exchange.
        domain: The domain for this exchange.
    """

    public_key: str
    domain: str

    @classmethod
    def from_dict(cls, msg: dict) -> "KeyExchange":
        """Creates a new KeyExchange message from dict.

        Arguments:
            msg: The message to convert.
        Returns:
            A KeyExchange object.
        """
        public_key = is_valid_wg_pubkey(msg.get("public_key"))
        domain = str(msg.get("domain"))
        if not is_valid_domain(domain):
            raise ValueError(f"Domain {domain} not in configured domains.")
        return cls(public_key=public_key, domain=domain)


@dataclasses.dataclass
class WorkerData:
    """A key exchange message. TODO

    Attributes:
        public_key: The public key for this exchange.
        domain: The domain for this exchange.
    """

    external_address: str
    port: int
    link_address: str
    public_key: str

    @classmethod
    def from_dict(cls, msg: dict) -> "WorkerData":
        """Creates a new WorkerData object from dict.

        Arguments:
            msg: The message to convert.
        Returns:
            A WorkerData object.
        """
        external_address = str(msg.get("ExternalAddress"))
        port = int(msg.get("Port"))
        link_address = str(link_address)
        public_key = is_valid_wg_pubkey(str(msg.get("PublicKey")))

        return cls(
            external_address=external_address,
            port=port,
            link_address=link_address,
            public_key=public_key,
        )


@dataclasses.dataclass
class BrokerStatus:
    """A key exchange message. TODO

    Attributes:
        public_key: The public key for this exchange.
        domain: The domain for this exchange.
    """

    online: bool


def _fetch_app_config() -> Flask_app:
    """Creates the Flask app from configuration.

    Returns:
        A created Flask app.
    """
    app = Flask(__name__)
    mqtt_cfg = config.get_config().mqtt
    app.config["MQTT_BROKER_URL"] = mqtt_cfg.broker_url
    app.config["MQTT_BROKER_PORT"] = mqtt_cfg.broker_port
    app.config["MQTT_USERNAME"] = mqtt_cfg.username
    app.config["MQTT_PASSWORD"] = mqtt_cfg.password
    app.config["MQTT_KEEPALIVE"] = mqtt_cfg.keepalive
    app.config["MQTT_TLS_ENABLED"] = mqtt_cfg.tls
    return app


"""
Setup
"""

app = _fetch_app_config()
mqtt = Mqtt(app)
# Register LWT to set worker status down when lossing connection
mqtt.client.will_set(
    TOPIC_BROKER_STATUS.format(broker=_HOSTNAME), 0, qos=1, retain=True
)
# worker_metrics holds data like connected peers per domain
worker_metrics = WorkerMetricsCollection()
# worker_data holds worker connectivity data relevant for clients per domain, like endpoint and pubkey
# { (worker, domain): WorkerData }
worker_data: Dict[Tuple[str, str], WorkerData] = {}
# broker_status tracks ther amount of broker instances running
# { broker: BrokerStatus }
broker_status: Dict[str, BrokerStatus] = {}


"""
HTTP section
"""

@app.route("/", methods=["GET"])
def index() -> str:
    """Returns main page"""
    return render_template("index.html")


@app.route("/api/v1/wg/key/exchange", methods=["POST"])
def wg_api_v1_key_exchange() -> Tuple[Response | Dict, int]:
    """Retrieves a new key and validates.
    Returns:
        Status message.
    """
    try:
        data = KeyExchange.from_dict(request.get_json(force=True))
    except Exception as ex:
        return {"error": {"message": str(ex)}}, 400

    key = data.public_key
    domain = data.domain
    # in case we want to decide here later we want to publish it only to dedicated gateways
    gateway = "all"
    logger.info(f"wg_api_v1_key_exchange: Domain: {domain}, Key:{key}")

    mqtt.client.publish(f"wireguard/{domain}/{gateway}", key)
    return {"Message": "OK"}, 200


@app.route("/api/v2/wg/key/exchange", methods=["POST"])
def wg_api_v2_key_exchange() -> Tuple[Response | Dict, int]:
    """Retrieves a new key, validates it and responds with a worker/gateway the client should connect to.

    Returns:
        Status message, Endpoint with address/domain, port pubic key and link address.
    """
    try:
        data = KeyExchange.from_dict(request.get_json(force=True))
    except Exception as ex:
        return {"error": {"message": str(ex)}}, 400

    key = data.public_key
    domain = data.domain
    # in case we want to decide here later we want to publish it only to dedicated gateways
    gateway = "all"
    logger.info(f"wg_api_v2_key_exchange: Domain: {domain}, Key:{key}")

    mqtt.client.publish(f"wireguard/{domain}/{gateway}", key)

    best_worker, diff, current_peers = worker_metrics.get_best_worker(domain)
    if best_worker is None:
        logger.warning(f"No worker online for domain {domain}")
        return {
            "error": {
                "message": "no gateway online for this domain, please check the domain value and try again later"
            }
        }, 400

    # Update number of peers locally to interpolate data between MQTT updates from the worker.
    # Increment it by the number of active brokers, assuming every broker gets roughly the same number of key exchange requests.
    # TODO fix data race
    online_brokers = sum(1 if broker.online else 0 for broker in broker_status.values())

    current_peers_domain = (
        worker_metrics.get(best_worker)
        .get_domain_metrics(domain)
        .get(CONNECTED_PEERS_METRIC, 0)
    )
    worker_metrics.update(
        best_worker,
        domain,
        CONNECTED_PEERS_METRIC,
        current_peers_domain + online_brokers,
    )
    logger.debug(
        f"Chose worker {best_worker} with {current_peers} connected clients ({diff})"
    )

    w_data = worker_data.get((best_worker, domain), None)
    if w_data is None:
        logger.error(f"Couldn't get worker endpoint data for {best_worker}/{domain}")
        return {"error": {"message": "could not get gateway data"}}, 500

    endpoint = {
        "Address": w_data.external_address,
        "Port": str(w_data.port),
        "AllowedIPs": [w_data.link_address],
        "PublicKey": w_data.public_key,
    }

    return {"Endpoint": endpoint}, 200


@app.route("/status", methods=["GET"])
def status() -> Tuple[Response | str, int]:
    response = ""
    response += f"online-brokers: {sum(1 if broker.online else 0 for broker in broker_status.values())}\n"
    response += f"online-workers: {sum(1 if worker.is_online() else 0 for worker in worker_metrics.data.values())}\n"
    response += f"total-peers: {worker_metrics.get_total_peer_count()}\n"

    return Response(response, mimetype="text/plain"), 200


"""
MQTT section
"""

@mqtt.on_connect()
def handle_mqtt_connect(
    client: mqtt_client.Client, userdata: bytes, flags: Any, rc: Any
) -> None:
    """Prints status of connect message."""
    # TODO(ruairi): Clarify current usage of this function.
    logger.debug(
        "MQTT connected to {}:{}".format(
            app.config["MQTT_BROKER_URL"], app.config["MQTT_BROKER_PORT"]
        )
    )
    client.subscribe("wireguard-metrics/#")
    client.subscribe(TOPIC_WORKER_STATUS.format(worker="+"))
    client.subscribe(TOPIC_WORKER_WG_DATA.format(worker="+", domain="+"))
    client.subscribe(TOPIC_BROKER_STATUS.format(broker="+"))
    client.publish(TOPIC_BROKER_STATUS.format(broker=_HOSTNAME), 1, qos=1, retain=True)


@mqtt.on_topic("wireguard-metrics/#")
def handle_mqtt_message_metrics(
    client: mqtt_client.Client, userdata: bytes, message: mqtt_client.MQTTMessage
) -> None:
    """Processes published metrics from workers."""
    logger.debug(
        f"MQTT message received on {message.topic}: {message.payload.decode()}"
    )
    _, domain, worker, metric = message.topic.split("/", 3)
    if not is_valid_domain(domain):
        logger.error(f"Domain {domain} not in configured domains")
        return

    if not worker or not metric:
        logger.error("Ignored MQTT message with empty worker or metrics label")
        return

    data = int(message.payload)

    logger.info(f"Update worker metrics: {metric} on {worker}/{domain} = {data}")
    worker_metrics.update(worker, domain, metric, data)


@mqtt.on_topic(TOPIC_WORKER_STATUS.format(worker="+"))
def handle_mqtt_message_worker_status(
    client: mqtt_client.Client, userdata: bytes, message: mqtt_client.MQTTMessage
) -> None:
    """Processes status messages from workers."""
    _, worker, _ = message.topic.split("/", 2)

    status = int(message.payload)
    if status < 1 and worker_metrics.get(worker).is_online():
        logger.warning(f"Marking worker as offline: {worker}")
        worker_metrics.set_offline(worker)
    elif status >= 1 and not worker_metrics.get(worker).is_online():
        logger.warning(f"Marking worker as online: {worker}")
        worker_metrics.set_online(worker)


@mqtt.on_topic(TOPIC_WORKER_WG_DATA.format(worker="+", domain="+"))
def handle_mqtt_message_data(
    client: mqtt_client.Client, userdata: bytes, message: mqtt_client.MQTTMessage
) -> None:
    """Processes data messages from workers.

    Stores them in a local dict"""
    _, worker, domain, _ = message.topic.split("/", 3)
    if not is_valid_domain(domain):
        logger.error(f"Domain {domain} not in configured domains.")
        return

    msg = json.loads(message.payload)
    if not isinstance(msg, dict):
        logger.error("Invalid worker data received for %s/%s: %s", worker, domain, msg)
        return
    try:
        w_data = WorkerData.from_dict(msg)
    except:
        logger.error("Invalid worker data received for %s/%s: %s", worker, domain, msg)
        return

    logger.info("Worker data received for %s/%s: %s", worker, domain, w_data)
    worker_data[(worker, domain)] = w_data


@mqtt.on_topic(TOPIC_BROKER_STATUS.format(broker="+"))
def handle_mqtt_message_broker_status(
    client: mqtt_client.Client, userdata: bytes, message: mqtt_client.MQTTMessage
) -> None:
    """Processes status messages from brokers."""
    _, broker, _ = message.topic.split("/", 2)

    status = int(message.payload)
    broker_status_data = broker_status.get(broker)
    if broker_status_data is None:
        # New broker
        if status >= 1:
            broker_status[broker] = BrokerStatus(True)
    elif status < 1 and broker_status_data.online:
        logger.warning(f"Marking broker as offline: {broker}")
        broker_status_data.online = False
    elif status >= 1 and not broker_status_data.online:
        logger.info(f"Marking broker as online: {broker}")
        broker_status_data.online = True


@mqtt.on_message()
def handle_mqtt_message(
    client: mqtt_client.Client, userdata: bytes, message: mqtt_client.MQTTMessage
) -> None:
    """Prints message contents."""
    logger.debug(
        f"MQTT message received on {message.topic}: {message.payload.decode()}"
    )


def is_valid_wg_pubkey(pubkey: str) -> str:
    """Verifies if key is a valid WireGuard public key or not.

    Arguments:
        pubkey: The key to verify.

    Raises:
        ValueError: If the Wireguard Key is invalid.

    Returns:
        The public key.
    """
    # TODO(ruairi): Refactor to return bool.
    if WG_PUBKEY_PATTERN.match(pubkey) is None:
        raise ValueError(f"Not a valid Wireguard public key: {pubkey}.")
    return pubkey


def join_host_port(host: str, port: str) -> str:
    """Concatenate a port string with a host string using a colon.
    The host may be either a hostname, IPv4 or IPv6 address.
    An IPv6 address as host will be automatically encapsulated in square brackets.

    Returns:
        The joined host:port string
    """
    if host.find(":") >= 0:
        return "[" + host + "]:" + port
    return host + ":" + port


if __name__ == "__main__":
    listen_host = None
    listen_port = None

    listen_config = config.get_config().broker_listen
    if listen_config is not None:
        listen_host = listen_config.host
        listen_port = listen_config.port

    serve(app, host=listen_host, port=listen_port)
