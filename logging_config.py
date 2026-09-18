"""
logging_config.py
==================
Production logging համակարգ՝ հիմնված "Production Logging Levels" ուղեցույցի
քաղաքականության վրա (Information / Warning / Error / Critical, structured JSON,
common field schema, secret masking):

  INFO      → սպասված, հաջողված գործողություններ (startup, login, request 200, job done)
  WARNING   → degraded, բայց համակարգը շարունակում է (slow query, retry, fallback,
              invalid input, token մոտ է ավարտվելուն)
  ERROR     → կոնկրետ operation/request ձախողվել է, app-ը շարունակում է աշխատել
  CRITICAL  → app-ը կամ կարևոր subsystem-ը (primary DB, encryption key, main storage)
              չի կարող նորմալ աշխատել

Log format՝ structured JSON, որպեսզի հեշտ լինի ուղարկել Loki/Elastic/Seq/CloudWatch։
Windows Event Viewer այս environment-ում կիրառելի չէ (Linux/Flask), բայց field
schema-ն (Severity, Service, RequestId, Operation, DurationMs, Exception...) նույնն է,
ինչ ուղեցույցի 15-րդ գլխում։
"""

import json
import logging
import logging.handlers
import os
import sys
import time
import traceback
import uuid
from werkzeug.exceptions import HTTPException

# =====================================================
# 🔹 CONFIG
# =====================================================

SERVICE_NAME = os.getenv("SERVICE_NAME", "CrosswordApi")
LOG_DIR = os.getenv("LOG_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
ENV = os.getenv("FLASK_ENV", "production")

os.makedirs(LOG_DIR, exist_ok=True)

# Ուղեցույցի գլուխ 8. այս key-երը երբեք պետք չէ լոգավորել ամբողջությամբ,
# այլ պետք է mask-ել, եթե պատահաբար հայտնվեն extra dict-ում.
_SENSITIVE_KEYS = {
    "password", "passwd", "token", "access_token", "refresh_token",
    "authorization", "auth_header", "api_key", "x-api-key", "secret",
    "client_secret", "private_key", "session", "cookie",
    "connection_string", "card_number", "cvv",
}


def _mask_sensitive(d):
    """Ռեկուրսիվ mask-ում է dict-ի զգայուն key-երը (password, token, secret...)."""
    if isinstance(d, dict):
        out = {}
        for k, v in d.items():
            if str(k).lower() in _SENSITIVE_KEYS:
                out[k] = "***MASKED***"
            else:
                out[k] = _mask_sensitive(v)
        return out
    if isinstance(d, list):
        return [_mask_sensitive(v) for v in d]
    return d


# =====================================================
# 🔹 JSON FORMATTER — ուղեցույցի 9/15-րդ գլխի schema-ով
# =====================================================

_RESERVED = set(logging.LogRecord(
    "", 0, "", 0, "", (), None
).__dict__.keys()) | {"message", "asctime"}


class StructuredJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "Timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "Severity": record.levelname if record.levelname != "CRITICAL" else "Critical",
            "Service": SERVICE_NAME,
            "Logger": record.name,
            "Message": record.getMessage(),
        }

        # extra={...}-ով փոխանցված custom դաշտերը (RequestId, Operation, DurationMs, և այլն)
        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in _RESERVED and not k.startswith("_")
        }
        if extras:
            payload.update(_mask_sensitive(extras))

        if record.exc_info:
            exc_type = record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
            payload["Exception"] = exc_type
            payload["Traceback"] = "".join(
                traceback.format_exception(*record.exc_info)
            )[-4000:]  # չափազանց երկար traceback-ը կտրում ենք

        return json.dumps(payload, ensure_ascii=False, default=str)


class HumanReadableFormatter(logging.Formatter):
    """Dev/local միջավայրի համար՝ ընթեռնելի console format։"""

    def format(self, record):
        base = super().format(record)
        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in _RESERVED and not k.startswith("_")
        }
        if extras:
            base += " | " + json.dumps(_mask_sensitive(extras), ensure_ascii=False, default=str)
        return base


# =====================================================
# 🔹 SETUP
# =====================================================

def setup_logging(app=None):
    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)
    root.handlers.clear()

    # --- Console handler (միայն եթե բացահայտ միացված է) ---
    if os.getenv("LOG_TO_CONSOLE", "false").lower() == "true":
        console = logging.StreamHandler(sys.stdout)
        if ENV == "development":
            console.setFormatter(HumanReadableFormatter(
                "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
            ))
        else:
            console.setFormatter(StructuredJsonFormatter())
        console.setLevel(LOG_LEVEL)
        root.addHandler(console)

    # --- app.log (INFO+) rotating ---
    app_handler = logging.handlers.RotatingFileHandler(
        os.path.join(LOG_DIR, "app.log"), maxBytes=10_000_000, backupCount=5, encoding="utf-8"
    )
    app_handler.setLevel(logging.INFO)
    app_handler.setFormatter(StructuredJsonFormatter())
    root.addHandler(app_handler)

    # --- error.log (ERROR+, ներառյալ CRITICAL) rotating ---
    error_handler = logging.handlers.RotatingFileHandler(
        os.path.join(LOG_DIR, "error.log"), maxBytes=10_000_000, backupCount=10, encoding="utf-8"
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(StructuredJsonFormatter())
    root.addHandler(error_handler)

    # Աղմկոտ 3rd-party logger-ները մի քիչ իջեցնել, որ INFO-ն չխեղդվի
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    if app is not None:
        app.logger.handlers = []
        app.logger.propagate = True
        app.logger.setLevel(LOG_LEVEL)

    logging.getLogger(__name__).info(
        "logging_initialized",
        extra={"Operation": "StartupLoggingInit", "LogLevel": LOG_LEVEL, "Env": ENV},
    )
    return root


# =====================================================
# 🔹 Flask request/response middleware (RequestId + duration)
# =====================================================

def register_request_logging(app, current_user_getter=None):
    """
    Ավելացնում է ամեն request-ին RequestId, չափում duration-ը, և INFO/WARNING/ERROR
    մակարդակով լոգավորում request_start / request_end իրադարձությունները,
    ինչպես ուղեցույցի "API / HTTP" աղյուսակում է սահմանված։

    current_user_getter՝ optional callable, որ վերադարձնում է user_id (g.current_user["sub"]).
    """
    from flask import request, g

    logger = logging.getLogger("app.http")

    @app.before_request
    def _start_request_log():
        g.request_id = request.headers.get("X-Request-ID", str(uuid.uuid4())[:12])
        g.start_time = time.time()
        logger.info(
            "request_start",
            extra={
                "Operation": "HttpRequest",
                "RequestId": g.request_id,
                "Method": request.method,
                "Endpoint": request.path,
                "Ip": request.headers.get("X-Forwarded-For", request.remote_addr),
            },
        )

    @app.after_request
    def _end_request_log(response):
        duration_ms = round((time.time() - g.get("start_time", time.time())) * 1000, 1)
        status = response.status_code

        user_id = None
        if current_user_getter:
            try:
                user_id = current_user_getter()
            except Exception:
                user_id = None

        log_fields = {
            "Operation": "HttpRequest",
            "RequestId": g.get("request_id"),
            "Method": request.method,
            "Endpoint": request.path,
            "StatusCode": status,
            "DurationMs": duration_ms,
            "UserId": user_id,
        }

        # Ուղեցույցի կանոն. 5xx → Error, 4xx/դանդաղ → Warning, մնացածը → Information
        if status >= 500:
            logger.error("request_end", extra=log_fields)
        elif status >= 400 or duration_ms > 2000:
            logger.warning("request_end", extra=log_fields)
        else:
            logger.info("request_end", extra=log_fields)

        response.headers["X-Request-ID"] = g.get("request_id", "")
        return response

    @app.errorhandler(Exception)
    def _handle_unhandled_exception(e):
        from flask import jsonify
        # HTTP սխալները (404, 403, 405...) թող մնան իրենց կոդով, 500 չդառնան
        if isinstance(e, HTTPException):
            return e
        logger.error(
            "unhandled_exception",
            extra={
                "Operation": "HttpRequest",
                "RequestId": g.get("request_id"),
                "Endpoint": request.path,
                "Method": request.method,
            },
            exc_info=True,
        )
        return jsonify({"success": False, "error": "Internal server error"}), 500


# =====================================================
# 🔹 Հարմար helper-ներ՝ business կոդից կանչելու համար
# =====================================================

def get_logger(name):
    return logging.getLogger(name)


def log_critical_startup_failure(logger, message, exception=None, **context):
    """
    Կիրառել ՄԻԱՅՆ, երբ app-ը/կարևոր subsystem-ը (primary DB, encryption key,
    main storage) չի կարող աշխատել: օրինակ՝ DB-ի ինիցիալիզացիան ձախողվել է startup-ում։
    """
    extra = dict(context)
    if exception:
        extra["Exception"] = type(exception).__name__
    logger.critical(message, extra=extra, exc_info=exception is not None)