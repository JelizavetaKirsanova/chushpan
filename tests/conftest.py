import os

import pytest
import yaml

from backend.config import CONFIG_FILE, Settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    """Keep tests away from the personal .env and the real data/ directory."""
    for key in list(os.environ):
        if key.startswith(("JARVIS_", "YOUTUBE_")):
            monkeypatch.delenv(key)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    # Keep the original Portuguese regression fixtures independent of the app's default.
    config = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    config["language"] = "pt-BR"
    config["stt"]["language"] = None
    config["assistant_name"] = "Jarvis"
    config["wakeword"]["phrase"] = None
    config["persona"]["file"] = None
    config["persona"]["max_sentences"] = 2
    config["media"]["provider"] = "spotify"  # original Spotify regression tests
    config["tts"]["engine"] = "piper"  # original audio fixtures; Silero has separate tests
    config["llm"]["model"] = "qwen3:8b"
    config["llm"]["think"] = False
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setitem(Settings.model_config, "yaml_file", config_file)


@pytest.fixture
def owner_env(monkeypatch):
    monkeypatch.setenv("JARVIS_OWNER_NAME", "Alice")
    monkeypatch.setenv("JARVIS_OWNER_GENDER", "feminino")
    monkeypatch.setenv("JARVIS_LATITUDE", "-25.43")
    monkeypatch.setenv("JARVIS_LONGITUDE", "-49.27")
    monkeypatch.setenv("JARVIS_TIMEZONE", "America/Sao_Paulo")
    monkeypatch.setenv("JARVIS_CITY", "Curitiba, PR, Brasil")
