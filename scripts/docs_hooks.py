from pathlib import Path
import shutil

def on_pre_build(config, **kwargs):
    root = Path(config.config_file_path).resolve().parent
    downloads = Path(config.docs_dir) / "downloads"
    downloads.mkdir(exist_ok=True)
    expected = set()
    for source in (root / "templates").iterdir():
        if not source.is_file():
            continue
        name = source.stem + ".txt" if source.suffix == ".md" else source.name
        expected.add(name)
        shutil.copyfile(source, downloads / name)
    for old in downloads.iterdir():
        if old.is_file() and old.name not in expected:
            old.unlink()
