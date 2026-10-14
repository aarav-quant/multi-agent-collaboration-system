"""Logging + X-Ray setup shared by every Lambda handler."""
import logging

from common.config import get_config


def init() -> logging.Logger:
    logging.getLogger().setLevel(logging.INFO)
    if not get_config().local_mode:
        try:
            # Patches boto3/botocore so DynamoDB, S3, EventBridge and Bedrock
            # calls appear as subsegments in the X-Ray trace of each Lambda.
            from aws_xray_sdk.core import patch_all
            patch_all()
        except Exception:  # noqa: BLE001 - tracing is optional
            logging.getLogger(__name__).warning("X-Ray SDK not available")
    return logging.getLogger("mas")
