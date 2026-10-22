"""Shared identifiers and session limits."""

import re


TENANT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ORDER_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
TOKEN_TTL = 7200
