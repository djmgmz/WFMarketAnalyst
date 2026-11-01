"""Item images: matching the catalog to the community database, downloading and serving."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from wfm import api
from wfm.catalog import upsert_item

from helpers import NOW, Notes


COMMUNITY_ITEMS = [  # the shape of the WFCD item list
    {"uniqueName": "/Lotus/Arcane/Energize", "name": "Arcane Energize", "imageName": "Energize.png"},
    {"uniqueName": "/Lotus/Projections/T1G", "name": "Lith G1 Intact", "imageName": "Unused.png"},
    {"uniqueName": "/Lotus/Projections/T1GBronze", "name": "Lith G1 Intact", "imageName": "RelicLith.png"},
    {"uniqueName": "/Lotus/Warframes/Ash", "name": "Ash Prime", "imageName": "AshPrime.png", "components": [
        {"uniqueName": "/Lotus/Recipes/AshPrimeHelmetComponent", "name": "Neuroptics", "imageName": "PrimeHelmet.png"},
        {"uniqueName": "/Lotus/Recipes/AshPrimeBlueprint", "name": "Blueprint", "imageName": "blueprint.png"},
    ]},
    {"uniqueName": "/Lotus/Mods/QuickThinkingBeginner", "name": "Quick Thinking", "imageName": "QuickThinking.jpg"},
    {"uniqueName": "/Lotus/Skins/A", "name": "Shared Name", "imageName": "A.png"},
    {"uniqueName": "/Lotus/Skins/B", "name": "Shared Name", "imageName": "B.png"},
    {"uniqueName": "/Lotus/Evil", "name": "Evil", "imageName": "../../etc/passwd"},
]


def test_images_match_by_game_id_relic_part_and_unique_name():
    from wfm.images import match_images

    catalog = [
        # Exact in-game id.
        ("a", "Arcane Energize", "/Lotus/Arcane/Energize"),
        # The exact id wins over the intact version.
        ("r", "Lith G1 Relic", "/Lotus/Projections/T1G"),
        # No match at all.
        ("r2", "Lith G2 Relic", "/Lotus/Projections/T1GX"),
        # Part blueprint -> the community's component.
        ("n", "Ash Prime Neuroptics Blueprint", "/Lotus/Recipes/AshPrimeHelmetBlueprint"),
        # Nested component id.
        ("b", "Ash Prime Blueprint", "/Lotus/Recipes/AshPrimeBlueprint"),
        # Unique name.
        ("q", "Quick Thinking", "/Lotus/Mods/QuickThinking"),
        # Ambiguous name: not guessed.
        ("s", "Shared Name", "/Lotus/Other"),
        # Unsafe file name: ignored.
        ("e", "Evil", "/Lotus/Evil"),
    ]
    assert match_images(COMMUNITY_ITEMS, catalog) == {
        "a": "Energize.png", "r": "Unused.png", "n": "PrimeHelmet.png", "b": "blueprint.png", "q": "QuickThinking.jpg"}
    relic_only = [("r", "Lith G1 Relic", "/Lotus/Projections/T1G")]
    # A base relic gets its intact version's image.
    assert match_images(COMMUNITY_ITEMS[2:3], relic_only) == {"r": "RelicLith.png"}


class FakeImageSource:
    def __init__(self, community, broken=()):
        self.community, self.broken, self.downloads = community, set(broken), []

    async def items(self):
        return self.community

    async def image(self, name):
        self.downloads.append(name)
        if name in self.broken:
            raise httpx.HTTPStatusError("404", request=httpx.Request("GET", name), response=httpx.Response(404))
        return f"bytes of {name}".encode()


def test_sync_images_downloads_each_file_once_and_resumes(conn, tmp_path, monkeypatch):
    from wfm import images

    monkeypatch.setattr(images, "PAUSE_SECONDS", 0)
    monkeypatch.setattr(images, "RETRIES", 0)
    for item_id, name, ref in [("a", "Arcane Energize", "/Lotus/Arcane/Energize"),
                               ("b1", "Ash Prime Blueprint", "/Lotus/Recipes/AshPrimeBlueprint"),
                               ("b2", "Other Prime Blueprint", "/Lotus/Recipes/OtherPrimeBlueprint"),
                               ("q", "Quick Thinking", "/Lotus/Mods/QuickThinking")]:
        upsert_item(conn, {"id": item_id, "slug": item_id, "name": name, "game_ref": ref, "tags": "[]"}, NOW, detail=False)
    conn.commit()
    community = [*COMMUNITY_ITEMS, {"uniqueName": "/Lotus/Recipes/OtherPrimeBlueprint", "name": "Blueprint",
                                    "imageName": "blueprint.png"}]
    folder = tmp_path / "images"

    first = FakeImageSource(community, broken={"QuickThinking.jpg"})
    notes = Notes()
    summary = asyncio.run(images.sync_images(conn, first, folder, notes))
    # The shared file is downloaded once.
    assert sorted(first.downloads) == ["Energize.png", "QuickThinking.jpg", "blueprint.png"]
    assert summary._asdict() == {"items": 4, "matched": 4, "files": 3, "downloaded": 2, "already_had": 0, "failed": 1}
    stored = dict(conn.execute("SELECT id, image FROM items"))
    # A failed download leaves the item without an image.
    assert stored == {"a": "Energize.png", "b1": "blueprint.png", "b2": "blueprint.png", "q": None}
    assert (folder / "blueprint.png").read_bytes() == b"bytes of blueprint.png" and not list(folder.glob(".*.part"))
    assert notes.done == 3

    # A stale value (the file was deleted) is cleared, and the missing files are fetched again.
    (folder / "Energize.png").unlink()
    again = FakeImageSource(community, broken={"Energize.png"})
    summary = asyncio.run(images.sync_images(conn, again, folder))
    # Resumes: only the missing files.
    assert sorted(again.downloads) == ["Energize.png", "QuickThinking.jpg"]
    assert (summary.already_had, summary.downloaded, summary.failed) == (1, 1, 1)
    stored = dict(conn.execute("SELECT id, image FROM items"))
    assert stored == {"a": None, "b1": "blueprint.png", "b2": "blueprint.png", "q": "QuickThinking.jpg"}


def test_http_image_source_identifies_itself_and_follows_the_cdn_redirect():
    from wfm.config import settings
    from wfm.images import HttpImageSource

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.url.path, request.headers["user-agent"]))
        if request.url.host == "api.warframestat.us":
            assert request.url.params["only"] == "uniqueName,name,imageName,components"
            return httpx.Response(200, json=COMMUNITY_ITEMS[:1])
        if request.url.host == "cdn.warframestat.us":
            return httpx.Response(301, headers={"location": "https://raw.githubusercontent.com" + request.url.path})
        if request.url.path.endswith("Energize.png"):
            return httpx.Response(200, content=b"png", headers={"content-type": "image/png"})
        return httpx.Response(200, text="<html>", headers={"content-type": "text/html"})

    async def run():
        async with HttpImageSource(transport=httpx.MockTransport(handler)) as source:
            items = await source.items()
            data = await source.image("Energize.png")
            with pytest.raises(ValueError):
                await source.image("Page.png")  # a web page instead of an image is refused
            return items, data

    items, data = asyncio.run(run())
    assert items == COMMUNITY_ITEMS[:1] and data == b"png"
    assert {ua for *_, ua in seen} == {settings.user_agent}


def test_item_images_are_served_and_listed(client, conn, tmp_path, monkeypatch):
    from wfm.config import Settings

    monkeypatch.setattr(api, "settings", Settings(db_path=tmp_path / "wfm.db"))
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "Energize.png").write_bytes(b"png bytes")
    conn.execute("UPDATE items SET image = 'Energize.png' WHERE id = 'i1'")
    conn.commit()

    items = {i["slug"]: i["image"] for i in client.get("/api/items").json()["items"]}
    assert items == {"arcane_energize": "Energize.png", "ash_prime_systems": None}
    resp = client.get("/api/images/Energize.png")
    assert resp.status_code == 200 and resp.content == b"png bytes" and "max-age" in resp.headers["cache-control"]
    assert client.get("/api/images/Missing.png").status_code == 404
    assert client.get("/api/images/..%2Fwfm.db").status_code == 404
