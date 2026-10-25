import pytest

from eventide.tools import regex_backend
from eventide.tools.filesystem import run_search_text


@pytest.mark.skipif(regex_backend.find_backend() is None, reason='rg unavailable')
def test_option_like_filenames_are_only_paths(tmp_path):
    (tmp_path / '-gignored').write_text('needle\n')
    result = run_search_text('needle', pattern='-g*', mode='regex', cwd=tmp_path)
    assert '-gignored:1: needle' in result
