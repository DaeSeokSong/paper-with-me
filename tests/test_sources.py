import pytest

from pwc import sources


def test_pick_data_files_prefers_gz():
    files = ["README.md", "papers-with-abstracts.json", "papers-with-abstracts.json.gz"]
    assert sources.pick_data_files(files) == ["papers-with-abstracts.json.gz"]


def test_pick_data_files_handles_nested_paths():
    files = [".gitattributes", "README.md", "data/evaluation-tables.json.gz"]
    assert sources.pick_data_files(files) == ["data/evaluation-tables.json.gz"]


def test_pick_data_files_falls_back_to_json():
    assert sources.pick_data_files(["README.md", "methods.json"]) == ["methods.json"]


def test_pick_data_files_returns_all_parquet_shards_in_order():
    files = [
        ".gitattributes", "README.md",
        "data/train-00001-of-00002.parquet", "data/train-00000-of-00002.parquet",
    ]
    assert sources.pick_data_files(files) == [
        "data/train-00000-of-00002.parquet", "data/train-00001-of-00002.parquet",
    ]


def test_pick_data_files_raises_when_missing():
    with pytest.raises(FileNotFoundError):
        sources.pick_data_files(["README.md", "data.csv"])


def test_resolve_url():
    url = sources.resolve_url("methods", "data/train-00000-of-00001.parquet")
    assert url == ("https://huggingface.co/datasets/pwc-archive/methods/"
                   "resolve/main/data/train-00000-of-00001.parquet")
