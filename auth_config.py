from dotenv import load_dotenv
load_dotenv()

import os

# AUTHSERVER_BASE_URL = os.environ.get(
#     "AUTHSERVER_BASE_URL",
#     "{{ MAINLOGIN_PUBLIC }}"
# )
AUTHSERVER_BASE_URL = os.environ.get(
    "AUTHSERVER_BASE_URL",
    ""
)

INTROSPECT_CLIENT_ID = os.environ.get(
    "INTROSPECT_CLIENT_ID",
    "myclient"
)

INTROSPECT_CLIENT_SECRET = os.environ.get(
    "INTROSPECT_CLIENT_SECRET",
    "secret"
)

VERIFY_TLS = os.environ.get(
    "AUTHSERVER_VERIFY_TLS",
    "true"
).lower() == "true"

INTROSPECTION_CACHE_TTL = int(
    os.environ.get("INTROSPECTION_CACHE_TTL", "60")
)

INTROSPECTION_TIMEOUT = float(
    os.environ.get("INTROSPECTION_TIMEOUT", "5.0")
)