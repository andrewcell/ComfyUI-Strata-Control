import asyncio
import json
import math
import os
import urllib.request
import urllib.error
from urllib.parse import urlsplit
from aiohttp import web
from server import PromptServer
import gc
import time

import psutil
import torch
import comfy.model_management as model_management

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
    CATEGORY = "Strata Control"
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
    CATEGORY = "Strata Control"
    OUTPUT_NODE = True
    ACTION = "unload"
    def run(self, base_url, timeout_s, api_key_env, trigger=None):
        result = call_strata(base_url, self.ACTION, timeout_s, api_key_env)
        return {"ui": {"text": [result]}, "result": (trigger, result)}

def synchronize_gpu():
    """Wait for pending GPU work before continuing."""
    if torch.cuda.is_available():
        # Includes GPUs exposed through the PyTorch CUDA interface.
        for index in range(torch.cuda.device_count()):
            torch.cuda.synchronize(index)

    elif (
        hasattr(torch, "xpu")
        and torch.xpu.is_available()
    ):
        for index in range(torch.xpu.device_count()):
            torch.xpu.synchronize(index)

    elif (
        hasattr(torch.backends, "mps")
        and torch.backends.mps.is_available()
    ):
        torch.mps.synchronize()


def memory_snapshot():
    """Return memory measurements in bytes."""
    ram = psutil.virtual_memory()

    snapshot = {
        "timestamp": time.time(),
        "ram": {
            "system_total_bytes": ram.total,
            "system_available_bytes": ram.available,
            "comfyui_rss_bytes": psutil.Process().memory_info().rss,
        },
        "gpu": [],
    }

    if torch.cuda.is_available():
        for index in range(torch.cuda.device_count()):
            with torch.cuda.device(index):
                free_bytes, total_bytes = torch.cuda.mem_get_info()

                snapshot["gpu"].append({
                    "backend": "cuda",
                    "index": index,
                    "name": torch.cuda.get_device_name(index),
                    "device_total_bytes": total_bytes,
                    "device_free_bytes": free_bytes,
                    "device_used_bytes": total_bytes - free_bytes,
                    "torch_allocated_bytes":
                        torch.cuda.memory_allocated(index),
                    "torch_reserved_bytes":
                        torch.cuda.memory_reserved(index),
                })

    else:
        snapshot["gpu_metrics_note"] = (
            "Detailed GPU metrics are implemented only for "
            "devices exposed through torch.cuda."
        )

    return snapshot


def cleanup_comfyui_memory():
    """
    Complete cleanup synchronously.

    Exceptions propagate: Strata must not load if cleanup fails.
    """
    synchronize_gpu()
    before = memory_snapshot()
    started = time.monotonic()

    model_management.unload_all_models()
    gc.collect()
    synchronize_gpu()

    model_management.soft_empty_cache(True)

    # Explicitly clear unused CUDA allocator caches on every device.
    if torch.cuda.is_available():
        for index in range(torch.cuda.device_count()):
            with torch.cuda.device(index):
                torch.cuda.empty_cache()

    gc.collect()
    synchronize_gpu()

    after = memory_snapshot()

    before_gpus = {
        gpu["index"]: gpu for gpu in before["gpu"]
    }

    delta = {
        # Positive means the measured memory usage decreased.
        "comfyui_rss_released_bytes": (
            before["ram"]["comfyui_rss_bytes"]
            - after["ram"]["comfyui_rss_bytes"]
        ),
        # System-wide metric; other processes can affect this value.
        "system_available_increase_bytes": (
            after["ram"]["system_available_bytes"]
            - before["ram"]["system_available_bytes"]
        ),
        "gpu": [],
    }

    for gpu in after["gpu"]:
        previous = before_gpus[gpu["index"]]

        delta["gpu"].append({
            "index": gpu["index"],
            "device_free_increase_bytes": (
                gpu["device_free_bytes"]
                - previous["device_free_bytes"]
            ),
            "torch_allocated_released_bytes": (
                previous["torch_allocated_bytes"]
                - gpu["torch_allocated_bytes"]
            ),
            "torch_reserved_released_bytes": (
                previous["torch_reserved_bytes"]
                - gpu["torch_reserved_bytes"]
            ),
        })

    return {
        "enabled": True,
        "status": "cleanup_completed",
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "before": before,
        "after": after,
        "delta": delta,
        "scope": (
            "ComfyUI model unloading, garbage collection, "
            "unused GPU cache release, and GPU synchronization. "
            "Live tensors and execution-cache references are retained."
        ),
    }
class ComfyUIMemoryCleanup:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "enabled": ("BOOLEAN", {"default": True}),
            },
            "optional": {
                "trigger": (ANY,),
            },
        }

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    RETURN_TYPES = (ANY, "STRING")
    RETURN_NAMES = ("passthrough", "result")
    FUNCTION = "run"
    CATEGORY = "Strata Control"
    OUTPUT_NODE = True

    def run(self, enabled=True, trigger=None):
        if enabled:
            report = cleanup_comfyui_memory()
        else:
            report = {
                "enabled": False,
                "status": "skipped",
            }

        result = json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
        )

        return {
            "ui": {"text": [result]},
            "result": (trigger, result),
        }

class StrataLoad(StrataUnload):
    ACTION = "load"

    @classmethod
    def INPUT_TYPES(cls):
        config = inputs()

        config["required"]["cleanup_before_load"] = (
            "BOOLEAN",
            {"default": False},
        )

        return config

    def run(
        self,
        base_url,
        timeout_s,
        api_key_env,
        cleanup_before_load=False,
        trigger=None,
    ):
        if cleanup_before_load:
            try:
                cleanup = cleanup_comfyui_memory()
            except Exception as exc:
                raise RuntimeError(
                    "Pre-load memory cleanup failed. "
                    "Strata /load was NOT called."
                ) from exc
        else:
            cleanup = {
                "enabled": False,
                "status": "skipped",
            }

        # Reached only after cleanup and GPU synchronization finish.
        before_load = memory_snapshot()
        load_started = time.monotonic()

        try:
            load_response = json.loads(
                call_strata(
                    base_url,
                    self.ACTION,
                    timeout_s,
                    api_key_env,
                )
            )
        except Exception as exc:
            failure = {
                "action": "load",
                "status": "load_failed",
                "cleanup": cleanup,
                "before_load": before_load,
                "error": str(exc),
            }

            raise RuntimeError(
                json.dumps(failure, ensure_ascii=False, indent=2)
            ) from exc

        report = {
            "action": "load",
            "status": "load_request_succeeded",
            "cleanup": cleanup,
            "load": {
                "elapsed_seconds": round(
                    time.monotonic() - load_started, 3
                ),
                "before": before_load,
                "after": memory_snapshot(),
                "api": load_response,
            },
        }

        result = json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
        )

        return {
            "ui": {"text": [result]},
            "result": (trigger, result),
        }

NODE_CLASS_MAPPINGS = {
    "StrataSimpleControlPanel": StrataControlPanel,
    "StrataSimpleUnload": StrataUnload,
    "StrataSimpleLoad": StrataLoad,
    "StrataControlMemoryCleanup": ComfyUIMemoryCleanup,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "StrataSimpleControlPanel": "Strata Control - Panel",
    "StrataSimpleUnload": "Strata Control - Unload",
    "StrataSimpleLoad": "Strata Control - Load",
    "StrataControlMemoryCleanup": "Strata Control - Memory Cleanup",
}
