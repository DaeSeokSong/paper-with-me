import pytest

from pwc import sources


def test_pick_data_file_prefers_gz():
    files = ["README.md", "papers-with-abstracts.json", "papers-with-abstracts.json.gz"]
    assert sources.pick_data_file(files) == "papers-with-abstracts.json.gz"


def test_pick_data_file_handles_nested_paths():
    files = [".gitattributes", "README.md", "data/evaluation-tables.json.gz"]
    assert sources.pick_data_file(files) == "data/evaluation-tables.json.gz"


def test_pick_data_file_falls_back_to_json():
    assert sources.pick_data_file(["README.md", "methods.json"]) == "methods.json"


def test_pick_data_file_raises_when_missing():
    with pytest.raises(FileNotFoundError):
        sources.pick_data_file(["README.md", "data.parquet"])


def test_resolve_url():
    url = sources.resolve_url("methods", "methods.json.gz")
    assert url == "https://huggingface.co/datasets/pwc-archive/methods/resolve/main/methods.json.gz"
