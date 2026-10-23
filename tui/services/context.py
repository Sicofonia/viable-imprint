"""Where the UI is running: the repository root, the config in use and how to
launch `pipeline.py`. Plain Python, no Textual.

Every manifest, prompt and template path in the pipeline is relative to the
repository root, and importing `pipeline` from anywhere else silently yields a
Click tree with no `s1b`/`s1d`/`s4` commands (ADR 023, Verification notes,
item 1). `Context.load()` therefore switches the working directory to the
repository root, and every subprocess the UI starts runs there.
"""
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG = "config.yaml"
DATA_DIRS = {
    "books_dir": "books",
    "intelligence_dir": "intelligence",
    "candidates_dir": "candidates",
    "homeostat_dir": "homeostat",
    "newsletter_dir": "newsletter",
}


def find_repo_root() -> Path:
    # tui/services/context.py -> the directory that holds pipeline.py
    return Path(__file__).resolve().parents[2]


class ConfigError(Exception):
    pass


@dataclass
class Context:
    repo_root: Path
    config_path: Path
    config: dict
    python: str = field(default_factory=lambda: sys.executable)
    # The script that is run as `pipeline.py`. Always the repository's own,
    # except in tests, which point it at tests/example_pipeline.py.
    pipeline_script: Path = None

    def __post_init__(self):
        if self.pipeline_script is None:
            self.pipeline_script = self.repo_root / "pipeline.py"

    @classmethod
    def load(cls, config_path=DEFAULT_CONFIG, *, repo_root: Path = None, pipeline_script: Path = None) -> "Context":
        """Resolve `config_path` against the directory the UI was launched from,
        then change into the repository root and read the config."""
        config_file = Path(config_path).resolve()
        root = (repo_root or find_repo_root()).resolve()
        os.chdir(root)
        ctx = cls(repo_root=root, config_path=config_file, config={}, pipeline_script=pipeline_script)
        ctx.reload_config()
        return ctx

    def reload_config(self) -> None:
        """Re-read the config file, so edits made in an editor show up without a restart."""
        try:
            with open(self.config_path, encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
        except OSError as exc:
            raise ConfigError(f"Cannot read {self.config_path}: {exc}") from exc
        except yaml.YAMLError as exc:
            raise ConfigError(f"Cannot parse {self.config_path}: {exc}") from exc
        self.config = loaded or {}

    @property
    def uses_default_config(self) -> bool:
        return self.config_path == (self.repo_root / DEFAULT_CONFIG).resolve()

    def data_dir(self, key: str) -> Path:
        """One of the five data folders (`books_dir` ...), absolute, as the pipeline resolves it."""
        if key not in DATA_DIRS:
            raise KeyError(key)
        return (self.repo_root / self.config.get(key, DATA_DIRS[key])).resolve()
