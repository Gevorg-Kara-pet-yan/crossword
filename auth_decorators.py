from functools import wraps
from flask import request, jsonify, g
from auth import introspect
import asyncio

def require_auth(*allowed_roles):
    allowed = set(allowed_roles)
    def decorator(fn):
        @wraps(fn)
        async def wrapped(*args, **kwargs):  # ← async
            authz = request.headers.get("Authorization", "")
            if not authz.lower().startswith("bearer "):
                return jsonify(error="Missing Bearer token"), 401
            user = await asyncio.to_thread(introspect, authz.split(" ", 1)[1].strip())
            if user is None:
                return jsonify(error="Invalid or expired token"), 401
            if allowed and not any(r in allowed for r in user["roles"]):
                return jsonify(error="Forbidden"), 403
            g.current_user = user
            return await fn(*args, **kwargs)  # ← await
        return wrapped
    return decorator