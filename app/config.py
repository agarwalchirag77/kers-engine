from __future__ import annotations

import json
from pathlib import Path

from .models import WeightConfig


CONFIG_DIR = Path(__file__).parent / "config"
CONNECTORS_DIR = Path(__file__).parent / "connectors"
WEIGHTS_PATH = CONFIG_DIR / "weights.json"
ZENDESK_PATH = CONNECTORS_DIR / "zendesk" / "settings.json"
PROMPTS_DIR = CONFIG_DIR / "prompts"

PROMPT_VERSION = "ers_prompt_v6"
DEFAULT_MODEL = "gpt-4o-2024-08-06"


def load_weight_config(path: Path = WEIGHTS_PATH) -> WeightConfig:
    with path.open() as f:
        data = json.load(f)
    return WeightConfig.model_validate(data)


def load_zendesk_config(path: Path = ZENDESK_PATH) -> dict:
    with path.open() as f:
        return json.load(f)


def load_prompt_template(version: str = PROMPT_VERSION) -> str:
    path = PROMPTS_DIR / f"{version}.txt"
    return path.read_text()
