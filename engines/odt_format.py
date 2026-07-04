import click
import yaml
from pathlib import Path

from lib import odt_writer, manifest

CLI_OPTIONS = [
    click.Option(
        ["--template"],
        type=click.Path(exists=True, dir_okay=False),
        default=None,
        help="Override the template .odt path from your style config "
             "(e.g. templates/6x9template.odt).",
    ),
]


def run(input_file: Path, root: Path, system: str, output_name: str, config: dict,
        *, style_config: str, template: str = None) -> Path:
    style_config_path = Path(style_config)
    if not style_config_path.exists():
        raise click.ClickException(
            f"Style config not found: {style_config_path}\n"
            "Copy templates/format_styles.example.yaml to that path and point it "
            "at your own .odt template."
        )
    with open(style_config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    template_path = Path(template) if template else Path(cfg["template"])
    style_map = cfg["styles"]

    # Mirror the language subdirectory only if input comes from <system>/translated/<lang>/
    # (a narrow, historical check preserved as-is from before this engine's
    # modularization — see ADR 001 — just shifted one index for the system prefix)
    rel = input_file.relative_to(root)
    parts = rel.parts
    if len(parts) >= 3 and parts[1] == "translated":
        output_dir = root / system / output_name / parts[2]
    else:
        output_dir = root / system / output_name

    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / (input_file.stem + ".odt")

    text = input_file.read_text(encoding="utf-8")
    odt_writer.write(text, output_file, template_path, style_map)

    manifest.update(
        root,
        **{f"{output_name}_{output_dir.name}": str(output_file.relative_to(root))},
    )

    click.echo(f"Saved: {output_file}")
    return output_file
