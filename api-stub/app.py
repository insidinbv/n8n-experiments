from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import iterate_in_threadpool

from datetime import datetime
from typing import Any, Dict, Optional, List
import os
import json
import random
import pathlib

try:
    import yaml  # optional for .yaml/.yml OpenAPI
except Exception:
    yaml = None

from jsonschema import Draft202012Validator, ValidationError

# =========================================================
# Test Sink API (Two Modes: SPEC or SINK)
# =========================================================
# Modes are mutually exclusive:
# - SPEC mode: if OPENAPI_PATH is set (or --openapi used when running python app.py)
# - SINK mode: otherwise (only /events and /health)
# Chaos applies in both modes. In SPEC mode, request validation is enabled by default
# and can be disabled with VALIDATE_INPUT=false (or --no-validate-input if running python app.py).
# =========================================================

# ---------- Effective mode detection (env-first; CLI may override in __main__) ----------
OPENAPI_PATH_ENV = os.getenv("OPENAPI_PATH")
MODE = "spec" if OPENAPI_PATH_ENV else "sink"  # default based on env for uvicorn imports

app = FastAPI(title=f"Test Sink API ({MODE.upper()} mode)")

# ---- Optional permissive CORS for local/browser tests (disabled by default) ----
if os.getenv("ENABLE_CORS", "").lower() in ("1", "true", "yes"):
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# -------------------- Config helpers --------------------

def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default

def _env_codes(name: str, default_codes: List[int]) -> List[int]:
    raw = os.getenv(name)
    if not raw:
        return default_codes
    out: List[int] = []
    for x in raw.split(","):
        x = x.strip()
        if x.isdigit():
            out.append(int(x))
    return out or default_codes

# -------------------- App state / configuration --------------------

# Chaos (both modes)
app.state.CHAOS        = _env_bool("CHAOS_ENABLED", False)
app.state.CHAOS_RATE   = max(0.0, min(1.0, _env_float("CHAOS_RATE", 0.2)))  # clamp [0,1]
app.state.CHAOS_CODES  = _env_codes("CHAOS_CODES", [400, 404, 422, 500, 505])

# Validation (SPEC mode only; default ON)
app.state.VALIDATE_INPUT = _env_bool("VALIDATE_INPUT", True)

# OpenAPI state
app.state.MODE = MODE
app.state.LOADED_OPENAPI: Optional[dict] = None
app.state.SPEC_PATH: Optional[str] = None

# -------------------- Logging --------------------

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
EVENT_LOG_FILE = os.path.join(LOG_DIR, "events.log")      # for /events sink (SINK mode)
REQ_LOG_FILE   = os.path.join(LOG_DIR, "requests.log")    # all requests/responses

def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"

def _append_line(path: str, entry: dict):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

def _append_event(entry: dict):
    _append_line(EVENT_LOG_FILE, entry)

def _append_reqlog(entry: dict):
    _append_line(REQ_LOG_FILE, entry)

def _preview_bytes(b: Optional[bytes], limit: int = 4000) -> Optional[str]:
    if b is None:
        return None
    try:
        s = b.decode("utf-8", errors="replace")
    except Exception:
        s = repr(b)
    return s if len(s) <= limit else (s[:limit] + "…")

# -------------------- Global request/response logging middleware --------------------

@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    start = datetime.utcnow()
    raw_req = await request.body()  # Starlette caches it for downstream

    try:
        response = await call_next(request)
    except Exception as exc:
        end = datetime.utcnow()
        entry = {
            "ts_start": start.isoformat() + "Z",
            "ts_end": end.isoformat() + "Z",
            "latency_ms": int((end - start).total_seconds() * 1000),
            "method": request.method,
            "path": request.url.path,
            "query": dict(request.query_params),
            "status": 500,
            "exception": repr(exc),
            "request_preview": _preview_bytes(raw_req),
        }
        _append_reqlog(entry)
        print(json.dumps(entry, ensure_ascii=False))
        raise

    resp_body = b""
    async for chunk in response.body_iterator:
        if isinstance(chunk, bytes):
            resp_body += chunk
        else:
            resp_body += str(chunk).encode("utf-8", errors="replace")
    response.body_iterator = iterate_in_threadpool(iter([resp_body]))
    try:
        response.headers["content-length"] = str(len(resp_body))
    except Exception:
        pass

    end = datetime.utcnow()
    entry = {
        "ts_start": start.isoformat() + "Z",
        "ts_end": end.isoformat() + "Z",
        "latency_ms": int((end - start).total_seconds() * 1000),
        "method": request.method,
        "path": request.url.path,
        "query": dict(request.query_params),
        "status": response.status_code,
        "request_preview": _preview_bytes(raw_req),
        "response_preview": _preview_bytes(resp_body),
    }
    _append_reqlog(entry)
    if response.status_code >= 400:
        print(json.dumps(entry, ensure_ascii=False))

    return response

# -------------------- OpenAPI / validation helpers (SPEC mode) --------------------

def _parse_openapi(path: str) -> dict:
    p = pathlib.Path(path)
    if not p.exists():
        raise FileNotFoundError(f"OpenAPI/Swagger file not found: {path}")
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        if yaml is None:
            raise RuntimeError("PyYAML not installed. Add 'pyyaml' to requirements.")
        return yaml.safe_load(text)
    return json.loads(text)

def _req_schema(op: Optional[dict]) -> Optional[dict]:
    if not op:
        return None
    rb = op.get("requestBody")
    if not rb:
        return None
    content = rb.get("content") or {}
    app_json = content.get("application/json")
    if not app_json:
        return None
    return app_json.get("schema")

def _validate_required_query(op: Optional[dict], query: Dict[str, Any]):
    if not op:
        return
    for p in (op.get("parameters") or []):
        if p.get("in") == "query" and p.get("required") is True:
            name = p.get("name")
            if name not in query:
                raise ValidationError(f"Missing required query parameter: {name}")

def _format_jsonschema_error(err: ValidationError) -> dict:
    instance_path = "/" + "/".join([str(p) for p in err.path]) if err.path else ""
    schema_path = "/".join([str(p) for p in err.schema_path]) if err.schema_path else ""
    out = {
        "message": err.message,
        "instancePath": instance_path,
        "schemaPath": schema_path,
        "validator": err.validator,
        "validatorValue": err.validator_value,
    }
    if err.validator == "type":
        out["expectedType"] = err.validator_value
        try:
            out["foundType"] = type(err.instance).__name__
        except Exception:
            pass
    if err.validator == "required" and isinstance(err.validator_value, list):
        out["missing"] = err.validator_value
    try:
        preview = err.instance
        s = json.dumps(preview, ensure_ascii=False) if not isinstance(preview, str) else preview
        if len(s) > 200:
            s = s[:200] + "…"
        out["foundValuePreview"] = s
    except Exception:
        pass
    return out

def _validate_json_detailed(instance, schema):
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
    return [_format_jsonschema_error(e) for e in errors]

def _error_payload(title: str, status: int, errors: list, opdef: dict = None):
    return {
        "error": title,
        "status": status,
        "operationId": (opdef or {}).get("operationId"),
        "summary": {"numErrors": len(errors)},
        "errors": errors,
    }

# -------------------- SINK MODE endpoints --------------------
if MODE == "sink":
    @app.get("/health")
    def health():
        return {
            "status": "up",
            "mode": app.state.MODE,
            "chaos": app.state.CHAOS,
            "rate": app.state.CHAOS_RATE,
            "codes": app.state.CHAOS_CODES,
            "validate_input": app.state.VALIDATE_INPUT,
        }

    @app.post("/events")
    async def events_sink(request: Request):
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid or missing JSON body.")

        if app.state.CHAOS and random.random() < app.state.CHAOS_RATE:
            status = random.choice(app.state.CHAOS_CODES)
            _append_event({
                "timestamp": _now(),
                "path": str(request.url.path),
                "method": "POST",
                "payload": payload,
                "status": status,
                "chaos": True
            })
            raise HTTPException(status_code=status, detail="Chaos mode injected error")

        _append_event({
            "timestamp": _now(),
            "path": str(request.url.path),
            "method": "POST",
            "payload": payload,
            "status": 200,
            "chaos": False
        })
        return JSONResponse({"status": "ok", "logged": True})

# -------------------- SPEC MODE dynamic routes --------------------

SUPPORTED_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}

def _install_openapi_routes(spec: dict):
    paths = spec.get("paths") or {}
    for raw_path, ops in paths.items():
        if not isinstance(ops, dict):
            continue
        for method, opdef in ops.items():
            mth = method.lower()
            if mth not in SUPPORTED_METHODS:
                continue

            route_path = raw_path
            op_for_route = opdef

            async def _handler(
                request: Request,
                __route_path=route_path,
                __method=mth,
                __opdef=op_for_route
            ):
                # Chaos (applies to every request in SPEC mode)
                if app.state.CHAOS and random.random() < app.state.CHAOS_RATE:
                    status = random.choice(app.state.CHAOS_CODES)
                    return JSONResponse({"error": "Chaos mode injected error", "status": status}, status_code=status)

                # Extract path params
                path_params = dict(request.path_params)

                # Parse body (if present)
                payload = None
                body_read = False
                try:
                    payload = await request.json()
                    body_read = True
                except Exception:
                    pass

                # Request validation (default ON; can be disabled)
                if app.state.VALIDATE_INPUT:
                    try:
                        _validate_required_query(__opdef, dict(request.query_params))
                        schema = _req_schema(__opdef)
                        if schema is not None:
                            if not body_read:
                                raw = await request.body()
                                if raw:
                                    try:
                                        payload = json.loads(raw.decode("utf-8"))
                                    except Exception:
                                        payload = None
                            if payload is None:
                                errors = [{"message": "Expected JSON body but none/invalid provided", "instancePath": ""}]
                                return JSONResponse(_error_payload("Request validation failed", 422, errors, __opdef), status_code=422)

                            errors = _validate_json_detailed(payload, schema)
                            if errors:
                                return JSONResponse(_error_payload("Request validation failed", 422, errors, __opdef), status_code=422)

                    except ValidationError as ve:
                        return JSONResponse(_error_payload("Request validation failed", 422, [_format_jsonschema_error(ve)], __opdef), status_code=422)

                # Success echo
                return JSONResponse({
                    "ok": True,
                    "method": __method.upper(),
                    "path": str(request.url.path),
                    "query": dict(request.query_params),
                    "path_params": path_params,
                    "payload": payload
                }, status_code=200)

            route_name = f"dyn_{mth}_{route_path}".replace("/", "_").replace("{", "").replace("}", "")
            app.add_api_route(route_path, _handler, methods=[mth.upper()], name=route_name)

# -------------------- Startup: if SPEC mode, load OPENAPI_PATH and install routes --------------------

@app.on_event("startup")
def _startup_load_openapi_if_spec_mode():
    if app.state.MODE != "spec":
        return
    spec_path = app.state.SPEC_PATH or os.getenv("OPENAPI_PATH")
    if not spec_path:
        raise RuntimeError("SPEC mode selected but no OPENAPI_PATH provided.")
    spec = _parse_openapi(spec_path)
    app.state.LOADED_OPENAPI = spec
    _install_openapi_routes(spec)

# -------------------- Optional CLI entry --------------------

if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="Test Sink API (SPEC or SINK mode)")
    parser.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8080")))
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")
    parser.add_argument("--openapi", type=str, default=os.getenv("OPENAPI_PATH"), help="Path to OpenAPI/Swagger (.json/.yaml) => SPEC mode")
    parser.add_argument("--chaos", action="store_true", help="Enable chaos mode (random non-2xx)")
    parser.add_argument("--chaos-rate", type=float, default=None, help="Probability 0.0–1.0 (default 0.2)")
    parser.add_argument("--chaos-codes", type=str, default=None, help="Comma-separated status codes, e.g. '400,404,422,500,505'")
    parser.add_argument("--no-validate-input", action="store_true", help="Disable input validation (SPEC mode only)")
    args = parser.parse_args()

    if args.openapi:
        app.state.MODE = "spec"
        app.title = "Test Sink API (SPEC mode)"
        app.state.SPEC_PATH = args.openapi
    else:
        app.state.MODE = "sink"
        app.title = "Test Sink API (SINK mode)"

    if args.chaos:
        app.state.CHAOS = True
    if args.chaos_rate is not None:
        app.state.CHAOS_RATE = max(0.0, min(1.0, args.chaos_rate))
    if args.chaos_codes:
        codes = [int(x.strip()) for x in args.chaos_codes.split(",") if x.strip().isdigit()]
        if codes:
            app.state.CHAOS_CODES = codes
    if args.no-validate-input:
        app.state.VALIDATE_INPUT = False

    if app.state.MODE == "spec":
        spec = _parse_openapi(app.state.SPEC_PATH)
        app.state.LOADED_OPENAPI = spec
        _install_openapi_routes(spec)

    uvicorn.run(app, host=args.host, port=args.port, reload=args.reload)
