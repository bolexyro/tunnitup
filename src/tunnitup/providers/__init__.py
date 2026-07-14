from tunnitup.providers.base import ProviderError, Tunnel, TunnelProvider
from tunnitup.providers.ngrok import NgrokProvider
from tunnitup.providers.outray import OutrayProvider


def create_provider(name: str) -> TunnelProvider:
    if name == "ngrok":
        return NgrokProvider()
    if name == "outray":
        return OutrayProvider()
    raise ProviderError(f"unsupported tunnel provider {name!r}")


__all__ = ["ProviderError", "Tunnel", "TunnelProvider", "create_provider"]
