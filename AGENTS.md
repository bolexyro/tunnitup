# Repository Guidelines

## About the Project

Tunnitup is an open-source local development tool for exposing multiple services through one permanent tunnel domain. It runs a local reverse proxy and routes incoming requests by URL path:

```text
https://example.ngrok.app/       -> localhost:3000
https://example.ngrok.app/api    -> localhost:8000
https://example.ngrok.app/hooks  -> localhost:4000
```

Only Tunnitup's proxy port is exposed through the tunnel. Users should not need Nginx, Docker, or another reverse proxy. The initial implementation will use Python so development can focus on infrastructure concepts, proxy correctness, provider supervision, and observability.

Ease of use is a core architectural requirement. Prefer short commands, sensible localhost defaults, port-number shorthand, readable errors, and progressive disclosure of advanced behavior. The proxy and provider core must remain usable without the TUI; the TUI will provide a friendlier control and observability layer over the same APIs.

## Current Implementation

Phase 1 is implemented under `src/tunnitup/`:

- `routing.py` parses routes and performs boundary-aware longest-prefix matching.
- `proxy.py` forwards HTTP requests and streams upstream responses.
- `cli.py` exposes `tunnitup proxy`, including repeatable routes and optional prefix stripping.

Tests live under `tests/`. The shortest supported command is `tunnitup proxy 3000`; add services with `--route /api=8000`.

Phase 2 hardens that core with streamed request and response bodies, finite upstream timeouts, distinct `502`/`504` failures, hop-by-hop header filtering, forwarded-context preservation, per-caller cookie isolation, handler cancellation on disconnect, and bounded graceful shutdown. Keep these guarantees covered by integration tests when changing proxy behavior.

Phase 3 adds strict `tunnitup.toml` configuration through `config.py`. The supported workflow is `tunnitup init`, `tunnitup validate`, then `tunnitup proxy`, which automatically discovers the file. Preserve concise route syntax, strict unknown-field rejection, overwrite protection, actionable errors, and unambiguous CLI/config precedence.

Phase 4 adds `tunnitup up`, the provider interface under `providers/`, and coordinated proxy/provider lifecycle management in `orchestration.py`. The ngrok adapter must preflight configuration, supervise its child process, discover the public URL through the local Agent API, redact credentials from diagnostics, report unexpected exits, and stop cleanly.

Phase 5 establishes the UI-independent observability core in `observability.py`. Proxy requests publish bounded, secret-safe completion records and active-request changes through `ObservationStore`; `HealthMonitor` checks only configured upstreams and retains the latest status per route. Consumers must subscribe to this API rather than reaching into aiohttp handlers.

Phase 7 adds the Textual interface in `tui.py`. `tunnitup tui` always opens the command center: users add routes with `A`, then launch with `S` through a provider-aware dialog for the static domain and proxy port. Keep discovery optional, surface occupied listener ports clearly, and never make the TUI own proxy/provider behavior.

## Current Roadmap

The canonical development roadmap is documented in `roadmap.html`. Work should follow its critical path:

1. Establish the Python package, CLI, tests, linting, and CI.
2. Build longest-prefix HTTP routing independently of tunnel providers.
3. Add streaming, forwarded-header handling, timeouts, failure handling, and graceful shutdown. **Complete.**
4. Introduce validated `tunnitup.toml` configuration and developer-friendly commands. **Complete.**
5. Integrate and supervise the installed ngrok CLI. This completes the first releasable version, `v0.1`. **Complete.**
6. Add request observability and health checks. **Core complete.**
7. Build the Textual TUI over the established core event model. **Complete.**
8. Add advanced HTTP behavior, including WebSockets and server-sent events.
9. Add OutRay and other providers through a provider-neutral interface.
10. Prepare cross-platform packages, documentation, and open-source release automation.

Keep proxy, routing, configuration, provider, CLI, and TUI concerns separate. Provider-specific behavior must not leak into the routing core.

## Scope Guardrails

Do not introduce Docker orchestration, built-in TLS termination, load balancing, an authentication gateway, a GUI, plugins, automatic edits to user projects, or a Rust port during the initial milestones. Prefer small vertical slices with observable acceptance criteria. The TUI must not be started until the proxy, provider lifecycle, and event model work without it.

## Development Environment

The product must not require Docker. Contributors should use the repository `Dockerfile` when the Docker engine is available:

```powershell
docker build -t tunnitup-dev .
docker run --rm --entrypoint pytest tunnitup-dev -q
```

If the container engine is unavailable, use the workspace-local environment with `uv sync --extra dev`, then run `rtk pytest -q` and `rtk ruff check .`. Never install project or system packages globally.
