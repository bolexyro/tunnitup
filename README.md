# Tunnitup

Tunnitup puts multiple local services behind one tunnel-ready HTTP endpoint. It is for development setups where ngrok, OutRay, or another provider gives you one permanent hostname but your application has a frontend, API, and other services.

## Quick start

Create a starter configuration for a frontend on port 3000 and an API on port 8000:

```powershell
tunnitup init 3000 --api 8000
tunnitup validate
tunnitup proxy
```

`tunnitup proxy` automatically discovers `tunnitup.toml` in the current directory. The generated file is deliberately small:

```toml
[proxy]
host = "127.0.0.1"
port = 8080
connect_timeout = 10
response_timeout = 60

[routes]
"/" = 3000
"/api" = { upstream = 8000, strip_prefix = true }
```

Use `tunnitup init --force` only when you intentionally want to replace an existing file. Unknown fields, invalid ports, malformed routes, and TOML syntax errors are reported by `tunnitup validate` before the proxy starts.

## Direct CLI usage

The shortest useful command exposes one service:

```powershell
tunnitup proxy 3000
```

Add services by repeating `--route`:

```powershell
tunnitup proxy 3000 --route /api=8000 --route /hooks=4000
```

Paths are preserved by default. If the API expects `/users` instead of `/api/users`, opt into prefix stripping:

```powershell
tunnitup proxy 3000 --route /api=8000 --strip-prefix /api
```

Ports are shorthand for `http://127.0.0.1:<port>`. Full upstream URLs also work:

```powershell
tunnitup proxy https://localhost:3000 --route /api=http://dev-api.local:8000
```

The proxy listens on `127.0.0.1:8080` by default. Point ngrok at that port manually during Phase 1:

```powershell
ngrok http 8080
```

Use `--config custom.toml` to load a different file. Listening and timeout flags can override configured settings, but config routes and CLI route mappings cannot be mixed; this keeps the effective routing table obvious.

## Proxy behavior

Tunnitup streams request and response bodies instead of loading them fully into memory. It removes hop-by-hop headers, preserves tunnel-provided `X-Forwarded-*` context, does not share upstream cookies between callers, and follows redirects transparently instead of consuming them.

Connection attempts time out after 10 seconds, and upstreams must produce data at least once every 60 seconds. These safe defaults can be adjusted for unusually slow development services:

```powershell
tunnitup proxy 3000 --connect-timeout 20 --response-timeout 120
```

An unavailable upstream returns `502 Bad Gateway`; an upstream that exceeds a configured timeout returns `504 Gateway Timeout`. Pressing `Ctrl+C` gives active handlers a bounded graceful-shutdown window.

## Development

Use the development container so contributors do not need to install project dependencies on the host:

```powershell
docker build -t tunnitup-dev .
docker run --rm tunnitup-dev --help
docker run --rm --entrypoint pytest tunnitup-dev -q
```

See [roadmap.html](roadmap.html) for the complete build plan.
