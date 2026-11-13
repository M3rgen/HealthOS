"""Path-like link labels must name the file or directory they open (BEL-04)."""

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def test_path_like_link_labels_match_target_names():
    pages = [ROOT / "README.md", ROOT / "CONTRIBUTING.md", *sorted(ROOT.glob("docs/**/*.md"))]
    problems = []
    for page in pages:
        for label, target in re.findall(
            r"\[([^\]\n]+)\]\(\s*<?([^\s()<>]+)>?(?:\s+[\"\'][^\n]*?[\"\'])?\s*\)",
            page.read_text(encoding="utf-8"),
        ):
            if "/" in label or ".md" in label:
                if Path(label.strip("` ")).name != Path(unquote(urlsplit(target).path)).name:
                    problems.append(f"{page.relative_to(ROOT)}: [{label}]({target})")
    assert not problems, "\n".join(problems)
