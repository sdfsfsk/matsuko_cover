"""Keep all cover engines and music sources wired to the new menu workflow."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from matsuko_cover_under_test import api as plugin_api
from matsuko_cover_under_test import main as plugin_main
from matsuko_cover_under_test.selection import CoverSelectionUI
from test_selection import FakeEvent, click, dispatch, wait_for_menu


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
    plugin.selection_ui = CoverSelectionUI()
    plugin.selection_ui.choose = AsyncMock(
        side_effect=[(1, song_reply), (0, model_reply)]
    )
    monkeypatch.setattr(plugin_api, "QQMusicAPI", lambda **_: music)
    handler = plugin._handle_qq_cover if qq_music else plugin._handle_cover

    output = [part async for part in handler(original, api_type=engine)]

    assert output == []
    payload = model_reply.bot.api.post_group_message.await_args.kwargs
    assert "歌曲二" in payload["markdown"]["content"]
    assert payload["msg_id"] == "model-choice-id"
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

    payload = event.bot.api.post_group_message.await_args.kwargs
    assert "官方示例音色" in payload["markdown"]["content"]
    assert event.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["rvc", "svc", "svcvc"])
@pytest.mark.parametrize("qq_music", [False, True])
@pytest.mark.parametrize("callback", [False, True])
async def test_commands_select_song_and_voice_using_native_cards(
    monkeypatch, engine, qq_music, callback
):
    plugin = plugin_main.MusicPlugin.__new__(plugin_main.MusicPlugin)
    plugin.enable_rvc = plugin.enable_svc = plugin.enable_svcvc = (
        plugin.enable_qqmusic
    ) = True
    plugin.llm_force_mode = plugin.disable_netease = False
    plugin.config = {}
    plugin.timeout = 30
    plugin.music_api_timeout = 10
    music = SimpleNamespace(
        fetch_data=AsyncMock(
            return_value=[
                {"name": "歌曲甲", "artists": "歌手甲"},
                {"name": "歌曲乙", "artists": "歌手乙"},
            ]
        ),
        close=AsyncMock(),
    )
    plugin.api = music
    plugin.get_models_display_list = lambda api_type: (
        "1. 音色甲\n2. 音色乙",
        ["voice_a", "voice_b"],
    )
    plugin._send_song = AsyncMock()
    plugin.selection_ui = CoverSelectionUI(
        config_getter=lambda **_: {"wake_prefix": ["#"]}
    )
    monkeypatch.setattr(plugin_api, "QQMusicAPI", lambda **_: music)
    monkeypatch.setattr(plugin_main, "QQ_MUSIC_AVAILABLE", True)
    command = ("qq" if qq_music else "") + engine
    event = FakeEvent(command + " 测试歌曲")
    if not callback:
        event.bot._connection = object()

    async def run():
        return [part async for part in getattr(plugin, command)(event)]

    async def choose(row_index, button_index, reply_id):
        payload = event.bot.api.post_group_message.await_args.kwargs
        action = payload["keyboard"]["content"]["rows"][row_index]["buttons"][
            button_index
        ]["action"]
        if callback:
            assert action["type"] == 1
            await click(event, action["data"], interaction_id=reply_id)
        else:
            assert action["type"] == 2 and action["enter"] is True
            reply = FakeEvent(action["data"], message_id=reply_id)
            reply.bot = event.bot
            await dispatch(reply)

    task = asyncio.create_task(run())
    try:
        await wait_for_menu(event)
        assert (
            "选择歌曲"
            in event.bot.api.post_group_message.await_args.kwargs["markdown"]["content"]
        )
        await choose(0, 1, "song-click")
        for _ in range(100):
            if event.bot.api.post_group_message.await_count >= 2:
                break
            await asyncio.sleep(0)
        text = event.bot.api.post_group_message.await_args.kwargs["markdown"]["content"]
        assert "选择音色" in text and "歌曲乙" in text
        await choose(0, 0, "voice-click")
        assert await asyncio.wait_for(task, 2) == []
        kwargs = plugin._send_song.await_args.kwargs
        assert kwargs["song"]["name"] == "歌曲乙" and kwargs["model_name"] == "voice_a"
        assert kwargs["api_type"] == engine
        payload = event.bot.api.post_group_message.await_args.kwargs
        assert "开始翻唱" in payload["markdown"]["content"]
        if callback:
            assert payload["event_id"] == "INTERACTION_CREATE:voice-click"
            assert "msg_id" not in payload
        else:
            assert payload["msg_id"] == "voice-click"
    finally:
        await plugin.selection_ui.close()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command", ["rvc", "svc", "svcvc", "qqrvc", "qqsvc", "qqsvcvc"]
)
async def test_empty_cover_command_displays_help_card(command):
    plugin = plugin_main.MusicPlugin.__new__(plugin_main.MusicPlugin)
    plugin.selection_ui = CoverSelectionUI()
    event = FakeEvent(command)
    handler = (
        plugin._handle_qq_cover if command.startswith("qq") else plugin._handle_cover
    )
    assert [
        part async for part in handler(event, api_type=command.removeprefix("qq"))
    ] == []
    assert (
        "用法"
        in event.bot.api.post_group_message.await_args.kwargs["markdown"]["content"]
    )
