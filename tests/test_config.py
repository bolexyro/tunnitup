from pathlib import Path

import pytest

from tunnitup.config import ConfigurationError, load_config, render_starter_config


def test_loads_shorthand_and_detailed_routes(tmp_path: Path) -> None:
    path = tmp_path / "tunnitup.toml"
    path.write_text(
        """
[proxy]
host = "localhost"
port = 9090
connect_timeout = 2.5
response_timeout = 15
shutdown_timeout = 4

[tunnel]
provider = "ngrok"
url = "https://example.ngrok.app"
startup_timeout = 8

[routes]
"/" = 3000
"/api" = { upstream = "http://api.local:8000/v1", strip_prefix = true }
""".strip(),
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.host == "localhost"
    assert config.port == 9090
    assert config.settings.connect_timeout == 2.5
    assert config.settings.response_timeout == 15
    assert config.settings.shutdown_timeout == 4
    assert config.tunnel.provider == "ngrok"
    assert config.tunnel.url == "https://example.ngrok.app"
    assert config.tunnel.startup_timeout == 8
    assert config.routes.match("/").upstream.port == 3000  # type: ignore[union-attr]
    api = config.routes.match("/api/users")
    assert api is not None
    assert api.strip_prefix is True
    assert str(api.target_url("/api/users")) == "http://api.local:8000/v1/users"


def test_starter_config_is_valid_and_can_include_an_api(tmp_path: Path) -> None:
    path = tmp_path / "tunnitup.toml"
    path.write_text(render_starter_config("5173", "8000"), encoding="utf-8")

    config = load_config(path)

    assert config.routes.match("/").upstream.port == 5173  # type: ignore[union-attr]
    assert config.routes.match("/api/users").strip_prefix is True  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ('[routes]\n"/" = 0', "port must be between 1 and 65535"),
        ('[routes]\n"/" = { upstream = 3000, strip_prefix = "yes" }', "true or false"),
        ('[routes]\n"/" = { strip_prefix = true }', "upstream is required"),
        ('[proxy]\nporrt = 8080\n[routes]\n"/" = 3000', "unknown field"),
        ('unexpected = true\n[routes]\n"/" = 3000', "unknown field"),
        (
            '[tunnel]\nprovider = "outray"\n[routes]\n"/" = 3000',
            "currently be 'ngrok'",
        ),
        (
            '[tunnel]\nurl = "http://example.ngrok.app/path"\n[routes]\n"/" = 3000',
            "must be HTTPS",
        ),
        ("[proxy", "invalid TOML"),
    ],
)
def test_configuration_errors_are_actionable(
    tmp_path: Path,
    contents: str,
    message: str,
) -> None:
    path = tmp_path / "tunnitup.toml"
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(ConfigurationError, match=message):
        load_config(path)


def test_missing_config_suggests_init(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="tunnitup init"):
        load_config(tmp_path / "missing.toml")
