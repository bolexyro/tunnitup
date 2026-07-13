from typing import Any

from typer.testing import CliRunner

from tunnitup.cli import app

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
