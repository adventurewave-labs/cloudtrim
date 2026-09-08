"""AWS client factory — the single seam between demo mode and live mode.

Every boto3 client in the engine is created here. When AWS_ENDPOINT_URL is
set (demo/sandbox), clients talk to the AWS-compatible emulator; when unset,
they talk to real AWS through the standard credential chain. No other code
path in the engine knows the difference — that is what makes the demo real:
the exact same scanning/remediation code runs against production accounts.
"""
from __future__ import annotations

import functools
from typing import Any

import boto3

from . import config


@functools.lru_cache(maxsize=32)
def client(service_name: str, region: str | None = None) -> Any:
    """Create a boto3 client for ``service_name``.

    Demo mode: endpoint override + static 'test' credentials.
    Live mode: default endpoint, standard credential chain (env vars,
    shared config, instance profile, SSO, ...).
    """
    region = region or config.AWS_REGION
    session_kwargs: dict[str, Any] = {}
    client_kwargs: dict[str, Any] = {}
    if config.AWS_PROFILE:
        session_kwargs["profile_name"] = config.AWS_PROFILE
    session = boto3.Session(region_name=region, **session_kwargs)
    if config.AWS_ENDPOINT_URL:
        client_kwargs.update(
            endpoint_url=config.AWS_ENDPOINT_URL,
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )
    return session.client(service_name, **client_kwargs)


def account_id() -> str:
    """Return the scanned account id via STS GetCallerIdentity (read-only)."""
    sts = client("sts")
    return sts.get_caller_identity()["Account"]


def session(region: str | None = None) -> boto3.Session:
    """Raw session for resource-style access."""
    region = region or config.AWS_REGION
    kwargs: dict[str, Any] = {}
    if config.AWS_PROFILE:
        kwargs["profile_name"] = config.AWS_PROFILE
    return boto3.Session(region_name=region, **kwargs)
