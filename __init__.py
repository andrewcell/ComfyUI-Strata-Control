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
import sys
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


def get_ram_snapshot():
    process = psutil.Process()
    memory_info = process.memory_info()
    system_memory = psutil.virtual_memory()

    private_commit = None

    if sys.platform == "win32":
        private_commit = getattr(memory_info, "private", None)

        if private_commit is None:
            private_commit = memory_info.vms

    return {
        "system_total_bytes": system_memory.total,
        "system_available_bytes": system_memory.available,
        "comfyui_rss_bytes": memory_info.rss,
        "comfyui_private_commit_bytes": private_commit,
    }


def released_bytes(before_value, after_value):
    if before_value is None or after_value is None:
        return None

    return max(0, before_value - after_value)

def memory_snapshot():
    """Return memory measurements in bytes."""
    ram = get_ram_snapshot()

    snapshot = {
        "timestamp": time.time(),
        "ram": ram,
        "pinned_memory": get_pinned_memory_snapshot(),
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

def diagnose_pinned_memory():
    stats_fn = getattr(
        torch.cuda.memory,
        "host_memory_stats",
        None,
    )

    if not torch.cuda.is_available() or not callable(stats_fn):
        return {
            "status": "unavailable",
            "torch_version": str(torch.__version__),
        }

    size_bytes = 16 * 1024 * 1024
    before = dict(stats_fn())

    tensor = torch.empty(
        size_bytes,
        dtype=torch.uint8,
        pin_memory=True,
    )

    during = dict(stats_fn())

    result = {
        "status": "completed",
        "torch_version": str(torch.__version__),
        "requested_bytes": size_bytes,
        "is_pinned": tensor.is_pinned(),
        "before": before,
        "during": during,
        "active_bytes_increase": (
            during["active_bytes.current"]
            - before["active_bytes.current"]
        ),
        "active_requests_increase": (
            during["active_requests.current"]
            - before["active_requests.current"]
        ),
    }

    del tensor

    result["after_release"] = dict(stats_fn())

    return result

def get_pinned_memory_snapshot():
    memory_module = getattr(torch.cuda, "memory", None)
    stats_fn = getattr(memory_module, "host_memory_stats", None)

    if not torch.cuda.is_available() or not callable(stats_fn):
        return {
            "supported": False,
            "status": "unavailable",
            "torch_version": str(torch.__version__),
        }

    try:
        stats = dict(stats_fn())

        allocated = stats.get("allocated_bytes.current")
        active = stats.get("active_bytes.current")

        valid = (
            isinstance(allocated, int)
            and isinstance(active, int)
            and 0 <= active <= allocated
        )

        return {
            "supported": True,
            "status": "ok" if valid else "inconsistent_stats",
            "torch_version": str(torch.__version__),
            "allocated_bytes": allocated,
            "active_bytes": active,
            "cached_bytes_estimate": (
                allocated - active if valid else None
            ),
            "raw_stats": stats,
        }

    except Exception as exc:
        return {
            "supported": True,
            "status": "error",
            "torch_version": str(torch.__version__),
            "error": f"{type(exc).__name__}: {exc}",
        }

def trim_process_memory():
    """Best-effort OS-specific memory trim for the current process."""
    import ctypes

    try:
        if sys.platform == "win32":
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL(
                "kernel32",
                use_last_error=True,
            )
            psapi = ctypes.WinDLL(
                "psapi",
                use_last_error=True,
            )

            get_current_process = kernel32.GetCurrentProcess
            get_current_process.argtypes = []
            get_current_process.restype = wintypes.HANDLE

            empty_working_set = psapi.EmptyWorkingSet
            empty_working_set.argtypes = [wintypes.HANDLE]
            empty_working_set.restype = wintypes.BOOL

            if not empty_working_set(get_current_process()):
                raise ctypes.WinError(ctypes.get_last_error())

            return {
                "status": "completed",
                "method": "windows_empty_working_set",
            }

        if sys.platform.startswith("linux"):
            # Resolve the allocator API already loaded in this process.
            libc = ctypes.CDLL(None, use_errno=True)
            malloc_trim = getattr(libc, "malloc_trim", None)

            if malloc_trim is None:
                return {
                    "status": "skipped",
                    "method": "linux_malloc_trim",
                    "reason": "malloc_trim is not available",
                }

            malloc_trim.argtypes = [ctypes.c_size_t]
            malloc_trim.restype = ctypes.c_int

            released = bool(malloc_trim(0))

            return {
                "status": "completed",
                "method": "linux_malloc_trim",
                "released_any": released,
            }

        return {
            "status": "skipped",
            "method": None,
            "reason": f"Unsupported platform: {sys.platform}",
        }

    except Exception as exc:
        return {
            "status": "error",
            "method": "os_memory_trim",
            "error": f"{type(exc).__name__}: {exc}",
        }

def clear_runtime_memory():
    gc.collect()

    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        torch.cuda.ipc_collect()

    else:
        accelerator = getattr(torch, "accelerator", None)

        if accelerator is not None and accelerator.is_available():
            accelerator.empty_cache()



def cleanup_comfyui_memory(trim_os_memory=False):
    """
    Release unused runtime memory, unload ComfyUI models,
    and synchronize GPU work before returning memory measurements.
    """
    synchronize_gpu()
    before = memory_snapshot()
    started = time.monotonic()

    # Release unused memory before unloading models.
    clear_runtime_memory()
    model_management.unload_all_models()
    gc.collect()
    model_management.soft_empty_cache()
    synchronize_gpu()

    # Optional OS-specific memory trim.
    os_memory_trim = {
        "status": "skipped",
        "reason": "Disabled",
    }

    if trim_os_memory:
        os_memory_trim = trim_process_memory()

    after = memory_snapshot()

    before_gpus = {
        gpu["index"]: gpu
        for gpu in before["gpu"]
    }

    gpu_delta = []

    for after_gpu in after["gpu"]:
        index = after_gpu["index"]
        before_gpu = before_gpus.get(index)

        if before_gpu is None:
            continue

        gpu_delta.append(
            {
                "index": index,
                "device_free_increase_bytes": max(
                    0,
                    after_gpu["device_free_bytes"]
                    - before_gpu["device_free_bytes"],
                ),
                "torch_allocated_released_bytes": released_bytes(
                    before_gpu.get("torch_allocated_bytes"),
                    after_gpu.get("torch_allocated_bytes"),
                ),
                "torch_reserved_released_bytes": released_bytes(
                    before_gpu.get("torch_reserved_bytes"),
                    after_gpu.get("torch_reserved_bytes"),
                ),
            }
        )

    delta = {
        "comfyui_rss_released_bytes": released_bytes(
            before["ram"]["comfyui_rss_bytes"],
            after["ram"]["comfyui_rss_bytes"],
        ),
        "comfyui_private_commit_released_bytes": released_bytes(
            before["ram"].get("comfyui_private_commit_bytes"),
            after["ram"].get("comfyui_private_commit_bytes"),
        ),
        "system_available_increase_bytes": max(
            0,
            after["ram"]["system_available_bytes"]
            - before["ram"]["system_available_bytes"],
        ),
        "gpu": gpu_delta,
    }

    return {
        "enabled": True,
        "status": "cleanup_completed",
        "method": "runtime_cleanup_with_gpu_synchronization",
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "before": before,
        "after": after,
        "delta": delta,
        "os_memory_trim": os_memory_trim,
       # "pinned_memory_diagnostic": diagnose_pinned_memory(),
        "scope": (
            "Garbage collection, unused GPU cache release, "
            "CUDA IPC collection when available, ComfyUI model unloading, "
            "and GPU synchronization. "
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
            report = cleanup_comfyui_memory(trim_os_memory=True)
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
                cleanup = cleanup_comfyui_memory(trim_os_memory=True)
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
