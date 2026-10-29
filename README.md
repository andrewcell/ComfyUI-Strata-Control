# ComfyUI-Strata-Control

A lightweight ComfyUI custom node pack for controlling an already-running Strata HTTP server.

It provides a manual control panel and dedicated Load / Unload nodes for workflow integration, with optional ComfyUI memory cleanup before loading Strata.

## Features

- Manual Status, Load, and Unload buttons.
- Dedicated workflow nodes for loading and unloading Strata models.
- Optional passthrough connections for establishing execution dependencies.
- Cache-disabled Load and Unload nodes.
- Optional pre-load memory cleanup, disabled by default.
- Sequential memory cleanup and GPU synchronization before sending the Load request.
- RAM and supported GPU memory measurements in the Load node's `result` output.
- Configurable server URL and request timeout.
- Optional Bearer authentication through a server-side environment variable.
- Server-side HTTP requests to avoid browser CORS restrictions.

## Included Nodes

| Node | Description |
|---|---|
| Strata Control Panel | Provides manual Status, Load, and Unload buttons. |
| Strata Unload | Sends an unload request when executed by the workflow. |
| Strata Load | Optionally cleans up ComfyUI memory, then sends a load request. |

## Requirements

- A working ComfyUI installation.
- Python 3.9 or newer.
- ComfyUI's existing `aiohttp`, `psutil`, and PyTorch dependencies.
- An already-running Strata server exposing the supported HTTP endpoints.
- Network access from the ComfyUI server to the Strata server.

No additional pip installation should be necessary in a standard compatible ComfyUI environment.

## Installation

### Using Git

Run the following command from your ComfyUI root directory:

```bash
git clone [https://github.com/andrewcell/ComfyUI-Strata-Control.git](https://github.com/andrewcell/ComfyUI-Strata-Control.git) custom_nodes/ComfyUI-Strata-Control
```

Restart ComfyUI, then hard-refresh your browser to reload frontend extensions.

### Manual Installation

1. Download and extract the repository.
2. Place the extracted folder inside `ComfyUI/custom_nodes`.
3. Ensure the folder is named `ComfyUI-Strata-Control`.
4. Restart ComfyUI.
5. Hard-refresh your browser.

The directory structure should look like this:

```text
ComfyUI/
└── custom_nodes/
    └── ComfyUI-Strata-Control/
        ├── __init__.py
        ├── README.md
        ├── images/
        │   └── workflow.png
        └── web/
            └── control_panel.js
```

Search for `Strata` in the node menu to add the nodes.

The current node category is `Strata Simple`.

## Configuration

All three nodes provide the following settings:

| Setting | Default | Description |
|---|---|---|
| `base_url` | `http://127.0.0.1:8080` | Root URL of the Strata server. Do not append `/v1`. |
| `timeout_s` | `300` | HTTP request timeout in seconds. Allowed range: 1–1800. |
| `api_key_env` | `STRATA_API_KEY` | Name of the ComfyUI server environment variable containing the Bearer token. |

The Strata Load node also provides:

| Setting | Default | Description |
|---|---|---|
| `cleanup_before_load` | `false` | Performs local ComfyUI memory cleanup and GPU synchronization before sending the Load request. |

`timeout_s` applies to the HTTP request. It does not limit the duration of local memory cleanup.

### Server Address

Requests originate from the ComfyUI server, not from your browser.

- Use `127.0.0.1` when Strata and ComfyUI run on the same host and network namespace.
- Use the Strata host's address when it runs on another machine.
- If ComfyUI runs inside a container, `127.0.0.1` refers to that container.

Use the server root URL:

```text
http://127.0.0.1:8080
```

Do not use:

```text
http://127.0.0.1:8080/v1
```

The current implementation rejects URLs containing embedded credentials, query strings, fragments, or non-root paths.

### Authentication

If your Strata server requires Bearer authentication, set an environment variable before starting ComfyUI.

Linux:

```bash
export STRATA_API_KEY="your-api-key"
python main.py
```

Windows PowerShell:

```powershell
$env:STRATA_API_KEY = "your-api-key"
python main.py
```

Leave `api_key_env` set to `STRATA_API_KEY`, or change it to another environment variable name.

The extension reads the variable and sends:

```http
Authorization: Bearer <value>
```

If the variable is missing or empty, no Authorization header is sent.

**Important:** The `api_key_env` field expects an environment variable name, not the actual API key.

Restart ComfyUI after changing the environment used to launch it.

## Strata Control Panel

Add a `Strata Control Panel` node and configure its server address.

The panel provides three buttons:

| Button | Request | Purpose |
|---|---|---|
| Status | `GET /health` | Queries the server's health response. |
| Load | `POST /load` | Requests model loading. |
| Unload | `POST /unload` | Requests model unloading. |

Load and Unload display a confirmation dialog before sending the request.

The `last_result` field displays the response or an error message. Successful responses are also logged to the browser console.

### Manual Request Behavior

Panel buttons send requests immediately. They do not enter the ComfyUI execution queue.

Use them only when it is safe to change Strata's model state. A manual request may conflict with an active Strata request or another workflow.

The panel's Load button does not perform pre-load memory cleanup. To use that feature, execute a Strata Load node with `cleanup_before_load` enabled.

Executing the Control Panel as part of a graph does not automatically load or unload Strata. Its graph function only passes through its optional input and returns an informational message.

The panel's `last_result` display is separate from its graph `result` output. Clicking a button updates the display, not the graph output.

## Strata Load and Strata Unload

These nodes send HTTP requests when ComfyUI executes them as part of a workflow.

### Inputs

| Input | Available On | Description |
|---|---|---|
| `base_url` | Load / Unload | Strata server root URL. |
| `timeout_s` | Load / Unload | HTTP request timeout. |
| `api_key_env` | Load / Unload | Environment variable name for optional authentication. |
| `cleanup_before_load` | Load only | Enables local memory cleanup before the Load request. Defaults to `false`. |
| `trigger` | Load / Unload | Optional dependency input. Its value is returned through `passthrough`. |

### Outputs

| Output | Description |
|---|---|
| `passthrough` | Returns the value received by `trigger` after the operation succeeds. |
| `result` | JSON-formatted operation information as a string. Load includes memory measurements and cleanup status. |

If `trigger` is not connected, `passthrough` returns `None`.

Both nodes are registered as output nodes, so they can execute without a downstream connection.

Their execution cache is disabled. Each time the node executes in a queued workflow, it sends a new HTTP request.

### Load Does Not Start the Server

`Strata Load` sends a model-loading request to an already-running Strata HTTP server.

It does not launch Strata or restart a stopped server process.

Likewise, `Strata Unload` requests model unloading; it does not terminate the Strata HTTP server process.

## Memory Cleanup Before Load

Set `cleanup_before_load` to `true` on the Strata Load node to enable cleanup.

The default is `false`, preserving the normal Load behavior.

### Cleanup Sequence

When enabled, the node performs the following steps sequentially:

1. Waits for pending GPU work to complete.
2. Records memory usage before cleanup.
3. Calls ComfyUI's model-unloading function.
4. Runs Python garbage collection.
5. Synchronizes GPU work.
6. Releases unused GPU memory caches.
7. Runs garbage collection and GPU synchronization again.
8. Records memory usage after cleanup.
9. Records memory usage immediately before the Load request.
10. Sends the Strata Load request.
11. Records memory usage after the Load response.
12. Returns the operation report through `result`.

The Load request is sent only after the local cleanup functions and GPU synchronization finish successfully.

If cleanup fails, the node raises an error and does not call Strata's `/load` endpoint.

### Cleanup Scope

The cleanup process attempts to release memory through:

- ComfyUI model unloading.
- Python garbage collection.
- ComfyUI's GPU cache cleanup function.
- Explicit release of unused CUDA allocator caches where available.

It does not forcibly delete:

- Live tensors still referenced by the workflow.
- Data retained by ComfyUI's execution cache.
- Objects retained by custom-node caches.
- The value connected to `trigger`.
- Memory used by other processes.
- GPU driver or runtime allocations.

Cleanup completion does not mean that all RAM or VRAM is empty.

No minimum free-memory threshold is enforced before loading Strata. Other processes may also allocate memory between cleanup and the Load request.

### Device Support

The cleanup process uses ComfyUI's memory-management functions.

Explicit GPU synchronization is implemented for CUDA, XPU, and MPS where those backends are available.

Detailed GPU memory measurements are currently implemented only for devices exposed through `torch.cuda`.

For other backends, the report includes RAM measurements and a note that detailed GPU metrics are unavailable.

## Memory Report

The Strata Load node returns a JSON-formatted report through its `result` output.

| Field | Description |
|---|---|
| `action` | Operation name: `load`. |
| `status` | `load_request_succeeded` when the Load request passes the implemented response checks. |
| `cleanup.enabled` | Whether pre-load cleanup was enabled. |
| `cleanup.status` | `skipped` or `cleanup_completed`. |
| `cleanup.elapsed_seconds` | Cleanup duration, when enabled. |
| `cleanup.before` | Memory measurements before cleanup. |
| `cleanup.after` | Memory measurements after cleanup. |
| `cleanup.delta` | Differences between the cleanup measurements. |
| `load.elapsed_seconds` | Duration of the Load request. |
| `load.before` | Memory measurements immediately before the Load request. |
| `load.after` | Memory measurements after the Load response. |
| `load.api` | Strata API response information. |

When cleanup is disabled, its before/after measurements and deltas are omitted. Load measurements are still recorded.

### RAM Measurements

| Field | Description |
|---|---|
| `system_total_bytes` | Total system RAM. |
| `system_available_bytes` | System-wide available RAM. |
| `comfyui_rss_bytes` | Resident memory of the ComfyUI process. |

### GPU Measurements

For each supported GPU:

| Field | Description |
|---|---|
| `device_total_bytes` | Total device memory reported by the runtime. |
| `device_free_bytes` | Available device memory. |
| `device_used_bytes` | Total device memory minus available device memory. |
| `torch_allocated_bytes` | Memory allocated through the ComfyUI process's PyTorch allocator. |
| `torch_reserved_bytes` | Memory reserved by the ComfyUI process's PyTorch allocator. |

### Interpreting Deltas

All memory values are recorded in bytes.

For cleanup deltas:

| Field | Meaning of a Positive Value |
|---|---|
| `comfyui_rss_released_bytes` | ComfyUI's measured resident memory decreased. |
| `system_available_increase_bytes` | System-wide available RAM increased. |
| `device_free_increase_bytes` | Available GPU memory increased. |
| `torch_allocated_released_bytes` | PyTorch allocated memory decreased. |
| `torch_reserved_released_bytes` | PyTorch reserved memory decreased. |

Negative values are possible.

System-wide RAM and device-wide GPU measurements can be affected by other processes. They do not isolate memory changes caused solely by this node.

The ComfyUI process's PyTorch measurements do not include allocations owned by a separate Strata process.

## Workflow Examples

### Unload Workflows

![Strata Unload workflow examples](images/unload.png)

Examples of unloading Strata before:

- Positive prompt encoding.
- Checkpoint loading.

Connect the upstream value to `trigger`, then connect `passthrough`
to the downstream node to establish an execution dependency.

Gating the positive prompt encoder does not delay independent
model-loader branches. Gate each relevant loader branch separately.

### Load Workflows

![Strata Load workflow examples](images/load.png)

Examples of loading Strata with:

- Pre-load memory cleanup enabled and `result` connected to Preview as Text.
- Image passthrough connected to Save Image, with cleanup disabled.

`cleanup_before_load` defaults to `false`. When enabled, local memory
cleanup and GPU synchronization finish before the Load request is sent.
The `result` output includes cleanup status, memory measurements,
and the Load API response.

In the passthrough example, Save Image waits for the Load request to
succeed. A cleanup or Load failure prevents that downstream save operation.

Cleanup does not guarantee that all RAM or VRAM is free. Live tensors
and execution-cache references may remain.

## Execution Ordering

Placing a Load or Unload node on the canvas does not establish its order relative to independent graph branches.

Use the `trigger` input and `passthrough` output to create an explicit dependency.

### Unload Before Text Encoding

```text
Prompt STRING
    │
    ▼
Strata Unload.trigger
    │
    └── passthrough
            │
            ▼
    CLIP Text Encode.text
```

The text encoder waits for the unload request to succeed.

However, this does not delay independent checkpoint, UNet, CLIP, or VAE loader branches.

### Unload Before Model Loading

Where supported, convert a model loader's filename widget into an input and route that value through the Unload node.

```text
Checkpoint name STRING
    │
    ▼
Strata Unload.trigger
    │
    └── passthrough
            │
            ▼
    Checkpoint Loader.ckpt_name
```

This establishes a dependency between the unload operation and that loader.

Keep the following limitations in mind:

- The loader must support converting the relevant widget to an input.
- The loader must accept the connection's type.
- The passthrough uses a wildcard convention that some custom nodes may reject.
- Independent loader branches must also be gated if they must wait.
- Gating only KSampler does not prevent its upstream loaders from running earlier.

This extension does not install a global “before queue execution” hook.

### Load After Image Generation

```text
Final IMAGE
    │
    ▼
Strata Load.trigger
    │
    └── passthrough
            │
            ▼
       Save Image.images
```

This makes the Load operation depend on the final image.

Enable `cleanup_before_load` on Strata Load if you want local memory cleanup to run before the Load request.

In this arrangement:

- Save Image runs only after cleanup, if enabled, and the Load request succeed.
- A cleanup or Load failure prevents that downstream Save Image node from executing.
- The image connected to `trigger` remains referenced and is not removed by cleanup.

If you need to save the image before attempting to load Strata, use a save node with a suitable completion or passthrough output, or another explicit dependency mechanism.

The standard Save Image node does not provide a passthrough output in this example.

## HTTP API
For endpoint details and server behavior, see the official
[Strata Engine and Server Reference](https://github.com/Niko1221/Strata/blob/main/docs/DETAILS.md).

For model loading, unloading, and GPU sharing, see
[Sharing the GPU with Other Programs](https://github.com/Niko1221/Strata/blob/main/docs/DETAILS.md#sharing-the-gpu-with-other-programs-optional).

The extension uses the following fixed endpoints:

| Operation | Method | Endpoint |
|---|---|---|
| Status | GET | `/health` |
| Load | POST | `/load` |
| Unload | POST | `/unload` |

Load and Unload send an empty JSON object:

```json
{}
```

The implementation:

- Treats HTTP errors and connection failures as failures.
- Treats a JSON response containing `ok: false` or a non-empty `error` field as a failure.
- Checks a top-level Boolean `loaded` field when present.
- Rejects `loaded: true` after an Unload request.
- Rejects `loaded: false` after a Load request.
- Does not automatically retry failed requests.
- Does not automatically fall back to `/v1/load` or `/v1/unload`.

A successful API response does not independently prove that all GPU memory has been reclaimed or that every internal background operation has finished.

Memory reports are snapshots taken around the operation, not continuous monitoring.

### ComfyUI Control Route

The manual panel uses this server-side route:

```text
POST /strata_simple/control
```

This internal route name remains unchanged regardless of the repository or installation folder name.

## Error Handling

For workflow nodes, a failed operation raises an error and prevents dependent nodes from executing.

For the manual panel, an error appears in the result field and an alert dialog.

### Cleanup Failure

If pre-load cleanup fails, Strata Load raises an error.

The `/load` request is not sent.

### Load Failure After Cleanup

If cleanup succeeds but the Load request fails, the node raises an error containing the cleanup report and Load error information.

Because execution failed, no normal `result` output is returned for that run.

### HTTP 409

A `409` response may indicate that Strata is busy.

This implementation reports the error without retrying or adding wait parameters. Allow the active request to finish before trying again.

### Request Timeout

Increase `timeout_s` if loading or unloading takes longer than the configured limit.

A client-side timeout does not cancel an operation already accepted by Strata. Check the server state before sending another request.

The HTTP timeout does not apply to local memory cleanup.

## Troubleshooting

### Nodes Do Not Appear

- Confirm that `__init__.py` is directly inside `ComfyUI-Strata-Control`.
- Check for an extra nested directory created during extraction.
- Restart ComfyUI.
- Inspect the ComfyUI startup log for import errors.
- Search for `Strata` or check the `Strata Simple` category.

### Cleanup Option Does Not Appear

- Confirm that the updated Python code is installed.
- Restart ComfyUI.
- Refresh the browser.
- Try adding a new Strata Load node.

The option is available only on Strata Load. It is not available on Strata Unload or the manual Control Panel.

### Panel Buttons Do Not Appear

- Hard-refresh the browser.
- Check the browser console for JavaScript errors.
- Confirm that `web/control_panel.js` exists.
- Confirm that the frontend extension is enabled.

### Connection Refused

- Confirm that Strata is running.
- Check the hostname and port.
- Verify connectivity from the ComfyUI host or container.
- Remember that the Load node does not start the Strata server process.

### HTTP 401 or 403

- Check whether your Strata server requires authentication.
- Confirm that the configured environment variable exists in the ComfyUI process environment.
- Confirm that the variable contains the correct API key.
- Restart ComfyUI after changing its launch environment.

### HTTP 404

Confirm that your Strata version exposes:

```text
GET  /health
POST /load
POST /unload
```

The current implementation does not support configurable endpoint paths.

Do not append `/v1` to `base_url`.

### Cleanup Completes, but Memory Usage Remains High

- Check whether the workflow still references model or image tensors.
- Check for data retained in ComfyUI's execution cache.
- Check for custom-node caches.
- Check memory usage by other processes.
- Review both device-wide and PyTorch-specific measurements.

Cleanup completion does not guarantee that a particular amount of memory is available.

### Strata Unloads, but Generation Still Runs Out of Memory

- Check whether independent ComfyUI model loaders ran before the unload request.
- Add execution dependencies to all relevant loader branches.
- Check whether another Strata client caused the model to load again.
- Verify actual GPU memory usage separately.

### Passthrough Connection Is Rejected

The passthrough ports use a wildcard type convention.

Some frontend versions or custom nodes may reject the connection. Verify compatibility with the destination node, especially when converting dropdown or filename widgets into inputs.

## Security

The manual control route accepts a target server URL from the browser and performs a server-side HTTP request.

Use this extension only in a trusted ComfyUI environment.

- Do not expose an unauthenticated ComfyUI instance to untrusted networks.
- Restrict access to the ComfyUI server.
- Keep API keys in server-side environment variables.
- Review workflows received from other users before executing them.
- Only send authenticated requests to trusted Strata server addresses.

This extension does not add its own authentication layer to the custom ComfyUI route.

## Limitations

- Requires an already-running Strata HTTP server.
- Does not start or stop the Strata process.
- ComfyUI model unloading is attempted only when `cleanup_before_load` is enabled on Strata Load.
- Does not forcibly delete live tensors, custom-node caches, or execution-cache references.
- Does not clear all RAM or VRAM.
- Does not enforce a minimum free-memory threshold before loading.
- Records memory snapshots rather than continuously monitoring memory.
- Detailed GPU memory reporting is limited to devices exposed through `torch.cuda`.
- Manual Control Panel loading does not perform pre-load memory cleanup.
- Does not prevent another client from loading Strata again.
- Does not serialize requests across separate nodes or clients.
- Does not provide a global pre-execution hook.
- Does not guarantee execution order without explicit graph dependencies.
- Does not include configurable API endpoint paths.
- Does not cancel remote operations when a local HTTP request times out.

## Validation Status

The initial package was generated and its Python syntax was checked.

The subsequent pre-load cleanup changes have not been integration-tested against a live ComfyUI and Strata installation.

Verify frontend compatibility, endpoint behavior, memory cleanup, memory reporting, and passthrough connections in your environment before relying on automated GPU handoff.