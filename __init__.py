import asyncio
import json
import math
import os
import urllib.request
import urllib.error
from urllib.parse import urlsplit
from aiohttp import web
from server import PromptServer

WEB_DIRECTORY = "./web"

class AnyType(str):
    def __ne__(self, other):
        return False

ANY = AnyType("*")

def call_strata(base_url, action, timeout, api_key_env="STRATA_API_KEY"):
    base_url = base_url.strip().rstrip("/")
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("Use an http(s) server URL without embedded credentials")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("Use the server root URL, e.g. http://127.0.0.1:8080 (without /v1)")
    paths = {"status": "/health", "load": "/load", "unload": "/unload"}
    if action not in paths:
        raise ValueError("Unknown action")
    headers = {"Content-Type": "application/json"}
    key = os.environ.get(api_key_env, "") if api_key_env else ""
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(base_url + paths[action], data=None if action == "status" else b"{}", headers=headers, method="GET" if action == "status" else "POST")
    try:
        with urllib.request.urlopen(req, timeout=int(timeout)) as resp:
            raw = resp.read(1024 * 1024).decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = raw
            if isinstance(payload, dict) and (payload.get("ok") is False or payload.get("error")):
                raise RuntimeError("Strata reported failure: " + json.dumps(payload, ensure_ascii=False))
            if isinstance(payload, dict) and isinstance(payload.get("loaded"), bool):
                if action == "unload" and payload["loaded"]:
                    raise RuntimeError("Strata still reports loaded=true")
                if action == "load" and not payload["loaded"]:
                    raise RuntimeError("Strata still reports loaded=false")
            return json.dumps({"action": action, "http_status": resp.status, "response": payload}, ensure_ascii=False)
    except urllib.error.HTTPError as e:
        detail = e.read(8192).decode("utf-8", errors="replace")
        raise RuntimeError(f"Strata HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Strata connection failed: {e.reason}") from e


def inputs():
    return {"required": {
        "base_url": ("STRING", {"default": "http://127.0.0.1:8080"}),
        "timeout_s": ("INT", {"default": 300, "min": 1, "max": 1800}),
        "api_key_env": ("STRING", {"default": "STRATA_API_KEY"}),
    }, "optional": {"trigger": (ANY,)}}

class StrataControlPanel:
    @classmethod
    def INPUT_TYPES(cls):
        return inputs()
    RETURN_TYPES = (ANY, "STRING")
    RETURN_NAMES = ("passthrough", "result")
    FUNCTION = "run"
    CATEGORY = "Strata Simple"
    def run(self, base_url, timeout_s, api_key_env, trigger=None):
        return (trigger, "Manual panel: use Status / Load / Unload buttons")

class StrataUnload:
    @classmethod
    def INPUT_TYPES(cls):
        return inputs()
    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")
    RETURN_TYPES = (ANY, "STRING")
    RETURN_NAMES = ("passthrough", "result")
    FUNCTION = "run"
    CATEGORY = "Strata Simple"
    OUTPUT_NODE = True
    ACTION = "unload"
    def run(self, base_url, timeout_s, api_key_env, trigger=None):
        result = call_strata(base_url, self.ACTION, timeout_s, api_key_env)
        return {"ui": {"text": [result]}, "result": (trigger, result)}

class StrataLoad(StrataUnload):
    ACTION = "load"

@PromptServer.instance.routes.post("/strata_simple/control")
async def strata_simple_control(request):
    try:
        data = await request.json()
        timeout = max(1, min(1800, int(data.get("timeout_s", 300))))
        result = await asyncio.to_thread(call_strata, data.get("base_url", "http://127.0.0.1:8080"), data.get("action", "status"), timeout, data.get("api_key_env", "STRATA_API_KEY"))
        return web.json_response({"ok": True, "result": result})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)

NODE_CLASS_MAPPINGS = {
    "StrataSimpleControlPanel": StrataControlPanel,
    "StrataSimpleUnload": StrataUnload,
    "StrataSimpleLoad": StrataLoad,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "StrataSimpleControlPanel": "Strata Control Panel",
    "StrataSimpleUnload": "Strata Unload",
    "StrataSimpleLoad": "Strata Load",
}
