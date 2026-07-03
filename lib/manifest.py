from pathlib import Path
import yaml


def load(book_dir: Path) -> dict:
    path = book_dir / "manifest.yaml"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def save(book_dir: Path, data: dict) -> None:
    path = book_dir / "manifest.yaml"
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True)


def update(book_dir: Path, **kwargs) -> None:
    data = load(book_dir)
    data.update(kwargs)
    save(book_dir, data)
