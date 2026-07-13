# Tunnitup

Tunnitup puts multiple local services behind one tunnel-ready HTTP endpoint. It is for development setups where ngrok, OutRay, or another provider gives you one permanent hostname but your application has a frontend, API, and other services.

## Phase 1: local reverse proxy

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
