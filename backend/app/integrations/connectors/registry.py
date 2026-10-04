"""Connector implementation registry."""

from .base import Connector, ConnectorError
from .database import RelationalConnector
from .file import FileConnector
from .rest import RestConnector
from .s3 import S3Connector
from .sftp import SftpConnector


def connector_for(connector_type: str, configuration: dict, credentials: dict, object_loader=None) -> Connector:
    implementations = {
        "FILE": FileConnector, "REST_API": RestConnector, "RELATIONAL_DB": RelationalConnector,
        "S3": S3Connector, "SFTP": SftpConnector,
    }
    implementation = implementations.get(connector_type.upper())
    if not implementation:
        raise ConnectorError("CONNECTOR_RUNTIME_UNAVAILABLE", f"{connector_type} is registered but its runtime is not installed")
    return implementation(configuration, credentials, object_loader)
