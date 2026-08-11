import logging
from pathlib import Path

from heybox_exporter.dom_parser import parse_mhtml
from heybox_exporter.exporter import ExportOptions, export_all


def test_exports_from_sample(tmp_path: Path) -> None:
    samples = list(Path(__file__).resolve().parents[1].glob("*.mhtml"))
    if not samples:
        return
    data, document = parse_mhtml(samples[0])
    output = export_all(
        data,
        ExportOptions(tmp_path, download_post_images=False, download_comment_images=False),
        logging.getLogger("test"),
        document,
    )
    files = list(output.iterdir())
    assert any(path.suffix == ".md" for path in files)
    assert any(path.suffix == ".html" for path in files)
    assert any(path.suffix == ".json" for path in files)
