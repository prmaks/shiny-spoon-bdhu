from __future__ import annotations

import json

from dom.__main__ import main


def test_cli_ingest_pick_stats(cfg, monkeypatch, capsys):
    monkeypatch.setenv("DOM_DATA_DIR", str(cfg.data_dir))
    monkeypatch.setenv("DOM_DB_PATH", str(cfg.db_path))
    monkeypatch.setenv("DOM_CACHE_DIR", str(cfg.cache_dir))
    assert main(["ingest"]) == 0
    out = capsys.readouterr().out
    assert "арты: 3" in out and "контрольная сумма не совпадает" in out
    assert main(["pick", "--count", "2", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["artworks"]) == 2
    assert main(["stats"]) == 0
    assert "Артов: 3" in capsys.readouterr().out
