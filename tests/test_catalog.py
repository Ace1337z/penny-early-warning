"""Model catalogue: persistence, aliases and the candidate pool."""

from __future__ import annotations

from penny.ai.catalog import ModelCatalog


def test_starting_catalogue_is_loaded(tmp_path):
    catalog = ModelCatalog(tmp_path / "catalog.json")
    assert len(catalog.all_ids()) > 20
    assert catalog.get("glm-5.1") is not None
    assert catalog.get("auto") is not None  # present, but an alias


def test_add_remove_persist(tmp_path):
    path = tmp_path / "catalog.json"
    catalog = ModelCatalog(path)
    catalog.add("brand-new", 2.5)
    reloaded = ModelCatalog(path)
    assert reloaded.multiplier_of("brand-new") == 2.5
    assert reloaded.remove("brand-new") is True
    assert ModelCatalog(path).get("brand-new") is None


def test_enable_disable(tmp_path):
    catalog = ModelCatalog(tmp_path / "catalog.json")
    assert catalog.enable("glm-5.1", False) is True
    assert catalog.get("glm-5.1").enabled is False
    catalog.enable("glm-5.1", True)
    assert catalog.get("glm-5.1").enabled is True


def test_pool_excludes_aliases_and_over_limit(tmp_path):
    catalog = ModelCatalog(tmp_path / "catalog.json")
    pool = catalog.pool(max_multiplier=4.0)
    assert "auto" not in pool
    assert "gpt-5.6" not in pool          # 5x, above the limit
    assert "glm-5.1" in pool
    assert catalog.multiplier_of("gpt-5.6") == 5.0


def test_premium_pool_allows_named_models(tmp_path):
    catalog = ModelCatalog(tmp_path / "catalog.json")
    pool = catalog.premium_pool(4.0, ["gpt-5.6"])
    assert "gpt-5.6" in pool
    assert "gpt-5.6-sol" not in pool
