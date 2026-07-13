from tunnitup.providers.base import ProviderError, Tunnel, TunnelProvider
from tunnitup.providers.ngrok import NgrokProvider


def create_provider(name: str) -> TunnelProvider:
    if name == "ngrok":
        return NgrokProvider()
    raise ProviderError(f"unsupported tunnel provider {name!r}")


__all__ = ["ProviderError", "Tunnel", "TunnelProvider", "create_provider"]
