from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from tunnitup.cli import app
from tunnitup.proxy import ROUTES_KEY

runner = CliRunner()


def test_proxy_uses_bounded_shutdown_and_handler_cancellation(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_run_app(_app: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("tunnitup.cli.web.run_app", fake_run_app)

    result = runner.invoke(
        app,
        [
            "proxy",
            "3000",
            "--connect-timeout",
            "1",
            "--response-timeout",
            "2",
        ],
    )

    assert result.exit_code == 0
    assert captured["handler_cancellation"] is True
    assert captured["shutdown_timeout"] == 10.0


def test_timeout_options_reject_non_positive_values() -> None:
    result = runner.invoke(app, ["proxy", "3000", "--connect-timeout", "0"])

    assert result.exit_code == 2
    assert "Invalid value" in result.output


def test_init_creates_a_valid_config_without_overwriting_it(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "5173", "--api", "8000"])

    assert result.exit_code == 0
    assert "Created tunnitup.toml" in result.output
    assert Path("tunnitup.toml").exists()
    assert '"/" = 5173' in Path("tunnitup.toml").read_text(encoding="utf-8")

    repeated = runner.invoke(app, ["init"])
    assert repeated.exit_code == 2
    assert "already exists" in repeated.output
    assert "--force" in repeated.output


def test_validate_shows_effective_routes(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)
    Path("tunnitup.toml").write_text(
        '[proxy]\nport = 9090\n[routes]\n"/" = 3000\n',
        encoding="utf-8",
    )

    result = runner.invoke(app, ["validate"])

    assert result.exit_code == 0
    assert "Configuration is valid" in result.output
    assert "http://127.0.0.1:9090" in result.output
    assert "http://127.0.0.1:3000" in result.output


def test_proxy_automatically_loads_config_and_accepts_setting_overrides(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run_app(proxy_app: Any, **kwargs: Any) -> None:
        captured["app"] = proxy_app
        captured.update(kwargs)

    monkeypatch.setattr("tunnitup.cli.web.run_app", fake_run_app)
    monkeypatch.chdir(tmp_path)

    Path("tunnitup.toml").write_text(
        '[proxy]\nport = 9090\nresponse_timeout = 15\n[routes]\n"/" = 3000\n',
        encoding="utf-8",
    )
    result = runner.invoke(app, ["proxy", "--port", "9191"])

    assert result.exit_code == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9191
    assert captured["app"][ROUTES_KEY].match("/").upstream.port == 3000


def test_proxy_without_routes_or_config_suggests_init(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["proxy"])

    assert result.exit_code == 2
    assert "tunnitup init" in result.output


def test_explicit_config_cannot_be_mixed_with_cli_routes() -> None:
    result = runner.invoke(app, ["proxy", "3000", "--config", "custom.toml"])

    assert result.exit_code == 2
    assert "cannot be combined" in result.output
