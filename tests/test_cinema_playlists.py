import pytest

from backend.cinema.store import CinemaStore


def test_custom_playlists_keep_membership_and_do_not_duplicate_library_items(tmp_path):
    path = tmp_path / "assistant.sqlite"
    store = CinemaStore(path)
    source = store.save_source(
        {"name": "Каталог", "base_url": "https://example.org", "adapter": "manual"}
    )
    first = store.add(
        {
            "title": "Первый фильм",
            "year": 2026,
            "source_id": source["id"],
            "url": "https://example.org/one",
        }
    )
    second = store.add(
        {
            "title": "Второй фильм",
            "year": 2025,
            "source_id": source["id"],
            "url": "https://example.org/two",
        }
    )

    playlist = store.playlist_create("  На выходные  ")
    store.playlist_item(playlist["id"], first["id"])
    store.playlist_item(playlist["id"], first["id"])
    store.playlist_item(playlist["id"], second["id"])
    assert store.playlists()[0]["name"] == "На выходные"
    assert set(store.playlists()[0]["item_ids"]) == {first["id"], second["id"]}
    assert len(store.library()) == 2
    with pytest.raises(ValueError, match="уже есть"):
        store.playlist_create("на выходные")

    store.close()
    reopened = CinemaStore(path)
    assert set(reopened.snapshot()["playlists"][0]["item_ids"]) == {first["id"], second["id"]}
    reopened.playlist_item(playlist["id"], first["id"], "remove")
    assert reopened.playlists()[0]["item_ids"] == [second["id"]]
    reopened.delete(first["id"])
    assert reopened.playlists()[0]["item_ids"] == [second["id"]]
    reopened.playlist_delete(playlist["id"])
    assert reopened.playlists() == []
    reopened.close()


def test_custom_playlist_names_and_actions_are_validated(tmp_path):
    store = CinemaStore(tmp_path / "assistant.sqlite")
    with pytest.raises(ValueError, match="название"):
        store.playlist_create("  ")
    with pytest.raises(ValueError, match="60 символов"):
        store.playlist_create("a" * 61)
    with pytest.raises(ValueError, match="Неизвестное действие"):
        store.playlist_item("missing", "missing", "move")
    store.close()
