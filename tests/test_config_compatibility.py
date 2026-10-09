import pytest
from pydantic import ValidationError

from rotor.config import Settings


def test_retired_chat_mcp_settings_in_dotenv_are_ignored(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "PROJECT_NAME=Configured Rotor\n"
        "ROTOR_MINDAGENT_MCP_COMMAND=/old/rotor-mcp\n"
        'ROTOR_MINDAGENT_MCP_ARGS=["--old-argument"]\n'
        "ROTOR_MINDAGENT_MCP_CWD=/old/working-directory\n"
    )

    settings = Settings(_env_file=env_file)

    assert settings.PROJECT_NAME == "Configured Rotor"
    assert not any("MINDAGENT" in name for name in settings.model_dump())
    assert settings.ROTOR_CONTROL_API_URL.endswith("/api/control/v1")


def test_other_unknown_dotenv_settings_remain_forbidden(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("ROTOR_MINDAGENT_MCP_TYPO=unexpected\n")

    with pytest.raises(ValidationError) as error:
        Settings(_env_file=env_file)

    assert any(
        item["type"] == "extra_forbidden"
        and item["loc"] == ("ROTOR_MINDAGENT_MCP_TYPO",)
        for item in error.value.errors()
    )
