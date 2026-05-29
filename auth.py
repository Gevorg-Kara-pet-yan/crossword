import hashlib
import threading
from typing import Optional, Dict, Any, List

import httpx
from cachetools import TTLCache

from auth_config import (
    AUTHSERVER_BASE_URL, INTROSPECT_CLIENT_ID,
    INTROSPECT_CLIENT_SECRET, VERIFY_TLS,
    INTROSPECTION_CACHE_TTL, INTROSPECTION_TIMEOUT,
)

_cache      = TTLCache(maxsize=10_000, ttl=INTROSPECTION_CACHE_TTL)
_cache_lock = threading.Lock()

_http = httpx.Client(
    timeout=httpx.Timeout(INTROSPECTION_TIMEOUT, connect=3.0),
    verify=VERIFY_TLS,
)

def _cache_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

def _as_list(v) -> List[str]:
    if v is None:       return []
    if isinstance(v, list): return [str(x) for x in v]
    return [str(v)]

def introspect(token: str) -> Optional[Dict[str, Any]]:
    if not token:
        return None

    key = _cache_key(token)

    with _cache_lock:
        cached = _cache.get(key)
    if cached is not None:
        return cached

    try:
        resp = _http.post(
            f"{AUTHSERVER_BASE_URL}/connect/introspect",
            data={"token": token, "token_type_hint": "access_token"},
            auth=(INTROSPECT_CLIENT_ID, INTROSPECT_CLIENT_SECRET),
            headers={"Accept": "application/json"},
        )
    except httpx.HTTPError:
        return None

    if resp.status_code != 200:
        return None

    try:
        data = resp.json()
    except ValueError:
        return None

    if not data.get("active"):
        return None

    user = {
        "sub":   data.get("sub"),
        "name":  data.get("name") or data.get("preferred_username"),
        "email": data.get("email"),
        "roles": _as_list(data.get("role")),
        "raw":   data,
    }

    with _cache_lock:
        _cache[key] = user

    return user