"""The citation file names the release at the top of the changelog."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_citation_version_and_date_match_the_latest_changelog_entry():
    # Each push is a release, so a stale citation would cite the wrong instructions
    latest = re.search(r"^## \[(\d+\.\d+\.\d+)\] - (\d{4}-\d{2}-\d{2})$", (ROOT / "CHANGELOG.md").read_text(), re.M)
    assert latest, "CHANGELOG.md has no '## [x.y.z] - YYYY-MM-DD' entry"
    citation = (ROOT / "CITATION.cff").read_text()
    assert f'version: "{latest.group(1)}"' in citation
    assert f'date-released: "{latest.group(2)}"' in citation
