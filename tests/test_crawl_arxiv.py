import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import crawl_arxiv  # noqa: E402


class ArxivFetchTests(unittest.TestCase):
    def test_invalid_xml_returns_empty_list(self):
        response = Mock()
        response.text = "<feed><entry>"
        response.raise_for_status.return_value = None

        with patch.object(crawl_arxiv.requests, "get", return_value=response):
            self.assertEqual(crawl_arxiv.fetch_arxiv_papers("cs.AI"), [])


if __name__ == "__main__":
    unittest.main()
