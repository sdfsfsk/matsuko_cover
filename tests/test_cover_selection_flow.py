"""Keep all cover engines and music sources wired to the new menu workflow."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from matsuko_cover_under_test import api as plugin_api
from matsuko_cover_under_test import main as plugin_main
from matsuko_cover_under_test.selection import CoverSelectionUI
from test_selection import FakeEvent


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["rvc", "svc", "svcvc"])
@pytest.mark.parametrize("qq_music", [False, True])
async def test_cover_sources_use_latest_selection_event(monkeypatch, engine, qq_music):
    plugin = plugin_main.MusicPlugin.__new__(plugin_main.MusicPlugin)
    songs = [
        {"name": "歌曲一", "artists": "歌手一"},
        {"name": "歌曲二", "artists": "歌手二"},
    ]
    music = SimpleNamespace(fetch_data=AsyncMock(return_value=songs), close=AsyncMock())
    plugin.api = music
    plugin.config = {}
    plugin.music_api_timeout = 10
    plugin.timeout = 30
    plugin._engine_display_name = lambda name: name
    plugin.get_models_display_list = lambda api_type: (
        "1. 音色甲\n2. 音色乙",
        ["model_a", "model_b"],
    )
    plugin._send_song = AsyncMock()
    original = FakeEvent(("qq" if qq_music else "") + engine + " 测试歌曲")
    song_reply = FakeEvent("2", message_id="song-choice-id")
    model_reply = FakeEvent("1", message_id="model-choice-id")
    plugin.selection_ui = SimpleNamespace(
        choose=AsyncMock(side_effect=[(1, song_reply), (0, model_reply)])
    )
    monkeypatch.setattr(plugin_api, "QQMusicAPI", lambda **_: music)
    handler = plugin._handle_qq_cover if qq_music else plugin._handle_cover

    output = [part async for part in handler(original, api_type=engine)]

    assert output == []
    assert "歌曲二" in model_reply.sent[0].get_plain_text()
    assert plugin.selection_ui.choose.await_args_list[0].args[0] is original
    assert plugin.selection_ui.choose.await_args_list[1].args[0] is song_reply
    assert plugin.selection_ui.choose.await_args_list[1].args[2] == ["音色甲", "音色乙"]
    plugin._send_song.assert_awaited_once_with(
        event=model_reply,
        song=songs[1],
        model_name="model_a",
        key_shift=None if engine == "svcvc" else 0,
        api_type=engine,
    )
    if qq_music:
        music.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_song_selection_never_starts_inference():
    plugin = plugin_main.MusicPlugin.__new__(plugin_main.MusicPlugin)
    plugin.api = SimpleNamespace(
        fetch_data=AsyncMock(return_value=[{"name": "歌曲一", "artists": "歌手一"}])
    )
    plugin.music_api_timeout = 10
    plugin.timeout = 30
    plugin._send_song = AsyncMock()
    reply = FakeEvent("取消")
    plugin.selection_ui = SimpleNamespace(choose=AsyncMock(return_value=(None, reply)))

    output = [part async for part in plugin._handle_cover(FakeEvent("rvc 测试歌曲"))]

    assert output == []
    assert "已取消选歌" in reply.sent[0].get_plain_text()
    plugin._send_song.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler", ["refresh_rvc_models", "refresh_svc_models", "refresh_svcvc_profiles"]
)
async def test_refresh_lists_are_visible_on_qq_official(handler):
    plugin = plugin_main.MusicPlugin.__new__(plugin_main.MusicPlugin)
    plugin.selection_ui = CoverSelectionUI()
    plugin._update_models_from_api = AsyncMock()
    plugin.get_models_display_list = lambda api_type: ("1. 官方示例音色", ["voice_1"])
    event = FakeEvent()

    _ = [part async for part in getattr(plugin, handler)(event)]

    assert len(event.sent) == 1
    assert "官方示例音色" in event.sent[0].get_plain_text()
    assert event.sent[0].use_markdown_ is False
