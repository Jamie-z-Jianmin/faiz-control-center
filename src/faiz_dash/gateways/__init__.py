"""Gateway protocols and CLI adapters for external services."""

from .colab import ColabCliGateway, parse_colab_sessions, parse_colab_usage
from .drive import (
    DriveGateway,
    GdriveCliDriveGateway,
    RcloneDriveGateway,
    create_drive_gateway,
    inspect_rclone_conf,
    parse_gdrive_quota,
)
from .rex import RexGateway

__all__ = [
    "ColabCliGateway",
    "DriveGateway",
    "GdriveCliDriveGateway",
    "RcloneDriveGateway",
    "RexGateway",
    "create_drive_gateway",
    "inspect_rclone_conf",
    "parse_colab_sessions",
    "parse_colab_usage",
    "parse_gdrive_quota",
]
