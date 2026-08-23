"""
POST /api/evaluate — run the policy gate against a manifest.

Implements PRD §5.4 (Touchpoint 3: Platform Engineering — Interactive Policy Gate).

Request body:
  {
    "manifest": "<YAML string>",   # optional; if omitted, uses the template default
    "scenario": "raw" | "golden_path",
    "template_id": "payments-ledger",
    "compare": false               # if true, returns both scenarios side-by-side
  }

Response (PRD §5.4):
  {
    "scenario": "raw",
    "denial_count": 42,
    "denials": [
      {"policy_id": "NW-K8S-001", "severity": "critical", "tool": "conftest",
       "subject": "Deployment/payments-ledger container \"payments-ledger\"",
       "message": "...", "remediation": "..."},
      ...
    ],
    "by_policy": {"NW-K8S-001": 2, ...},
    "duration_ms": 12,
    "engine": "conftest-cached" | "python-faithful-reimpl",
    "cached": true | false,
    "cold_start": true | false
  }
"""
from __future__ import annotations
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import _lib  # type: ignore  # noqa: E402


def _build_response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        },
        "body": json.dumps(body, default=str),
    }


def handler(request=None, body: dict | None = None) -> dict:
    """Evaluate a manifest against the policy gate."""
    cold_start = _lib.detect_cold_start()

    try:
        payload = body or {}
        if hasattr(request, "body") and request.body:
            try:
                payload = json.loads(request.body)
            except Exception:
                return _build_response(400, {"error": "invalid JSON body"})

        # PRD §5.6 case 3: empty manifest — validate client-side before sending.
        manifest_yaml = payload.get("manifest") or payload.get("yaml") or None
        scenario = payload.get("scenario", "raw")
        template_id = payload.get("template_id", "payments-ledger")
        compare = bool(payload.get("compare", False))

        # Validate scenario.
        if scenario not in {"raw", "golden_path"}:
            return _build_response(400, {"error": f"invalid scenario: {scenario}"})

        # PRD §5.6 case 4: conftest unavailable → fall back to cached results.
        # This is the default path on Vercel serverless.
        if compare:
            response = _lib.build_compare_response(cold_start=cold_start)
        else:
            response = _lib.build_evaluate_response(
                manifest_yaml=manifest_yaml,
                scenario=scenario,
                template_id=template_id,
                cold_start=cold_start,
            )
        return _build_response(200, response)

    except Exception as e:
        # PRD §5.6 case 1: conftest timeout → return partial results with a warning.
        # We don't have a real conftest to time out, but if our Python impl fails,
        # we return the cached 42-denial response with a warning flag.
        try:
            cached_response = _lib.build_evaluate_response(None, "raw", "payments-ledger", cold_start)
            cached_response["warning"] = f"python analyzer failed, returned cached: {e}"
            cached_response["cached"] = True
            return _build_response(200, cached_response)
        except Exception as fallback_err:
            return _build_response(500, {
                "error": f"evaluate failed and fallback also failed: {fallback_err}",
                "original_error": str(e),
            })


# Vercel Python SDK shape
try:
    from vercel_functions import Request, Response  # type: ignore

    async def POST(request: Request) -> Response:
        body = None
        if hasattr(request, "body") and request.body:
            try:
                body = json.loads(request.body) if isinstance(request.body, (str, bytes)) else request.body
            except Exception:
                pass
        result = handler(request, body)
        return Response(
            status_code=result["statusCode"],
            headers=result["headers"],
            body=result["body"],
            media_type="application/json",
        )

    async def GET(request: Request) -> Response:
        # GET returns the template list (useful for the dropdown).
        result = _build_response(200, {"templates": _lib.list_templates()})
        return Response(
            status_code=result["statusCode"],
            headers=result["headers"],
            body=result["body"],
            media_type="application/json",
        )

    async def OPTIONS(request: Request) -> Response:
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
            },
        )
except ImportError:
    pass
