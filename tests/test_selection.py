"""Exercise native QQ menus and the real AstrBot session-waiter lifecycle."""

import asyncio
import secrets
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from matsuko_cover_under_test.qq_callbacks import original_client
from matsuko_cover_under_test.selection import CoverSelectionUI, _Menu

from astrbot.api.message_components import Node
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.core.utils.session_waiter import FILTERS, USER_SESSIONS, SessionWaiter


class FakeEvent:
    def __init__(
        self,
        text="",
        sender="owner",
        origin="qq:GroupMessage:group",
        platform="qq_official",
        scene="group",
        message_id="input-1",
    ):
        self.message_str = text
        self.unified_msg_origin = origin
        self.sender = sender
        self.platform = platform
        self.sent = []
        self.stopped = False
        source = {
            "group": SimpleNamespace(group_openid="group-openid"),
            "c2c": SimpleNamespace(author=SimpleNamespace(user_openid=sender)),
            "channel": SimpleNamespace(channel_id="channel-id"),
            "dm": SimpleNamespace(guild_id="guild-id"),
            "unknown": SimpleNamespace(),
        }[scene]
        self.message_obj = SimpleNamespace(raw_message=source, message_id=message_id)
        self.bot = SimpleNamespace(
            api=SimpleNamespace(
                **{
                    name: AsyncMock(return_value={"id": "outgoing-message"})
                    for name in (
                        "post_group_message",
                        "post_c2c_message",
                        "post_message",
                        "post_dms",
                        "on_interaction_result",
                    )
                }
            )
        )

    def get_sender_id(self):
        return self.sender

    def get_platform_name(self):
        return self.platform

    def plain_result(self, text):
        return MessageEventResult().message(text)

    def chain_result(self, chain):
        return MessageEventResult(chain=chain)

    async def send(self, result):
        self.sent.append(result)

    def stop_event(self):
        self.stopped = True


async def dispatch(event):
    """Dispatch as AstrBot's built-in session controller does."""
    for session_filter in list(FILTERS):
        key = session_filter.filter(event)
        if key in USER_SESSIONS:
            await SessionWaiter.trigger(key, event)
            event.stop_event()


async def wait_for_menu(event, method="post_group_message"):
    for _ in range(30):
        if getattr(event.bot.api, method).await_count or event.sent:
            return
        await asyncio.sleep(0)
    raise AssertionError("Menu was never sent")


async def click(event, data, interaction_id=None, **overrides):
    """Deliver a real SDK Interaction without generating a user chat message."""
    from botpy.interaction import Interaction

    client = original_client(event.bot)
    source = event.message_obj.raw_message
    interaction_id = interaction_id or secrets.token_hex(8)
    payload = {
        "id": interaction_id,
        "type": 11,
        "group_openid": getattr(source, "group_openid", None),
        "group_member_openid": event.get_sender_id(),
        "user_openid": event.get_sender_id(),
        "guild_id": getattr(source, "guild_id", None),
        "channel_id": getattr(source, "channel_id", None),
        "data": {
            "type": 11,
            "resolved": {"button_data": data, "user_id": event.get_sender_id()},
        },
        **overrides,
    }
    interaction = Interaction(
        client.api, f"INTERACTION_CREATE:{interaction_id}", payload
    )
    await client.on_interaction_create(interaction)
    return interaction


@pytest.fixture(autouse=True)
def no_leaked_waiters():
    original_filters = list(FILTERS)
    original_sessions = dict(USER_SESSIONS)
    yield
    assert FILTERS == original_filters
    assert USER_SESSIONS == original_sessions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scene", "method", "destination", "direct"),
    [
        ("group", "post_group_message", "group_openid", False),
        ("c2c", "post_c2c_message", "openid", True),
        ("channel", "post_message", "channel_id", False),
        ("dm", "post_dms", "guild_id", False),
    ],
)
async def test_native_payload_has_owner_scoped_buttons_and_correct_route(
    scene, method, destination, direct
):
    event = FakeEvent(scene=scene)
    menu = _Menu(
        "选择歌曲",
        [f"很长的歌曲名称{i}以及演唱者" for i in range(12)],
        30,
        "menu-token",
        True,
    )
    await menu.send(event)
    payload = getattr(event.bot.api, method).await_args.kwargs
    assert destination in payload
    assert payload["msg_id"] == "input-1"
    assert "1\\. 很长的歌曲名称0" in payload["markdown"]["content"]
    assert "8\\. 很长的歌曲名称7" in payload["markdown"]["content"]
    assert "9\\. 很长的歌曲名称8" not in payload["markdown"]["content"]
    rows = payload["keyboard"]["content"]["rows"]
    assert len(rows) == 5
    assert all(len(row["buttons"]) <= 5 for row in rows)
    for row in rows:
        for button in row["buttons"]:
            assert len(button["render_data"]["label"]) <= 10
            assert button["action"]["type"] == 1
            assert "enter" not in button["action"]
            assert "reply" not in button["action"]
            assert button["action"]["permission"] == {
                "type": 0,
                "specify_user_ids": ["owner"],
            }
            assert button["action"]["data"].startswith("matsuko-cover:menu-token:")
    assert event.sent == []
    assert "点击按钮即可选择，无需再发送确认消息" in payload["markdown"]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [PermissionError("not allowed"), None, {"code": 304023}, {}]
)
async def test_native_failure_always_sends_visible_plain_choices(failure):
    event = FakeEvent()
    if isinstance(failure, Exception):
        event.bot.api.post_group_message.side_effect = failure
    else:
        event.bot.api.post_group_message.return_value = failure
    menu = _Menu("选歌", ["歌曲甲 - 歌手甲", "歌曲乙 - 歌手乙"], 30, "token", True)
    await menu.send(event)
    assert len(event.sent) == 1
    assert event.sent[0].use_markdown_ is False
    text = event.sent[0].get_plain_text()
    assert "1. 歌曲甲 - 歌手甲" in text and "2. 歌曲乙 - 歌手乙" in text
    assert "按钮暂不可用" in text
    assert not any(isinstance(part, Node) for part in event.sent[0].chain)
    await menu.send(event)
    assert event.bot.api.post_group_message.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform", "buttons", "node"),
    [
        ("qq_official", False, False),
        ("aiocqhttp", True, True),
        ("telegram", True, False),
    ],
)
async def test_other_modes_keep_a_readable_menu(platform, buttons, node):
    event = FakeEvent(platform=platform)
    await _Menu("选音色", ["音色一"], 30, "token", buttons).send(event)
    event.bot.api.post_group_message.assert_not_awaited()
    assert isinstance(event.sent[0].chain[0], Node) is node


@pytest.mark.asyncio
async def test_waiter_is_ready_when_the_first_button_becomes_visible():
    event = FakeEvent()

    async def send(**payload):
        data = payload["keyboard"]["content"]["rows"][0]["buttons"][0]["action"]["data"]
        await click(event, data, interaction_id="first-click")
        return {"id": "menu-message"}

    event.bot.api.post_group_message.side_effect = send
    ui = CoverSelectionUI()
    selected, reply = await ui.choose(event, "选歌", ["歌曲一"], 30)
    assert selected == 0
    assert reply._cover_callback_event_id == "INTERACTION_CREATE:first-click"
    event.bot.api.on_interaction_result.assert_awaited_once_with(
        interaction_id="first-click", code=0
    )
    assert not ui._pending
    await ui.close()


@pytest.mark.asyncio
async def test_pagination_uses_latest_reply_and_preserves_absolute_indices():
    event = FakeEvent()
    ui = CoverSelectionUI()
    task = asyncio.create_task(
        ui.choose(event, "选音色", [f"音色{i}" for i in range(12)], 30)
    )
    await wait_for_menu(event)
    payload = event.bot.api.post_group_message.await_args.kwargs
    data = payload["keyboard"]["content"]["rows"][-1]["buttons"][0]["action"]["data"]
    await click(event, data, interaction_id="page-click")
    next_payload = event.bot.api.post_group_message.await_args.kwargs
    assert next_payload["event_id"] == "INTERACTION_CREATE:page-click"
    assert "msg_id" not in next_payload
    assert "9\\. 音色8" in next_payload["markdown"]["content"]
    choice = next_payload["keyboard"]["content"]["rows"][0]["buttons"][1]["action"][
        "data"
    ]
    await click(event, choice, interaction_id="select-click")
    selected, reply = await task
    assert selected == 9
    assert reply._cover_callback_event_id == "INTERACTION_CREATE:select-click"
    await ui.close()


@pytest.mark.asyncio
async def test_unrelated_users_conversations_and_messages_are_not_consumed():
    event = FakeEvent()
    ui = CoverSelectionUI()
    task = asyncio.create_task(ui.choose(event, "选歌", ["歌曲一"], 30))
    await wait_for_menu(event)
    for other in [
        FakeEvent("1", sender="other"),
        FakeEvent("1", origin="qq:GroupMessage:other"),
        FakeEvent("聊天消息"),
    ]:
        await dispatch(other)
        assert not other.stopped
        assert not task.done()
    reply = FakeEvent("#1")
    await dispatch(reply)
    assert await task == (0, reply)


@pytest.mark.asyncio
async def test_old_song_button_does_not_select_a_model():
    ui = CoverSelectionUI()
    event = FakeEvent()
    song = asyncio.create_task(ui.choose(event, "选歌", ["歌曲一"], 30))
    await wait_for_menu(event)
    old_data = event.bot.api.post_group_message.await_args.kwargs["keyboard"][
        "content"
    ]["rows"][0]["buttons"][0]["action"]["data"]
    song_reply = FakeEvent("1", message_id="song-selection")
    await dispatch(song_reply)
    await song
    model = asyncio.create_task(
        ui.choose(song_reply, "选音色", ["音色一", "音色二"], 30)
    )
    await wait_for_menu(song_reply)
    stale = FakeEvent(old_data)
    await dispatch(stale)
    assert not stale.stopped and not model.done()
    model_reply = FakeEvent("2")
    await dispatch(model_reply)
    assert await model == (1, model_reply)


@pytest.mark.asyncio
async def test_same_group_can_have_menus_for_different_users():
    ui = CoverSelectionUI()
    one, two = FakeEvent(sender="one"), FakeEvent(sender="two")
    task_one = asyncio.create_task(ui.choose(one, "选歌", ["A", "B"], 30))
    task_two = asyncio.create_task(ui.choose(two, "选歌", ["A", "B"], 30))
    await wait_for_menu(one)
    await wait_for_menu(two)
    reply_two, reply_one = FakeEvent("2", sender="two"), FakeEvent("1", sender="one")
    await dispatch(reply_two)
    assert await task_two == (1, reply_two)
    assert not task_one.done()
    await dispatch(reply_one)
    assert await task_one == (0, reply_one)


@pytest.mark.asyncio
async def test_duplicate_menu_is_rejected_and_cancel_cleans_up():
    ui = CoverSelectionUI(buttons=False)
    event = FakeEvent()
    first = asyncio.create_task(ui.choose(event, "选歌", ["A"], 30))
    await wait_for_menu(event)
    with pytest.raises(ValueError, match="已有"):
        await ui.choose(event, "选歌", ["B"], 30)
    invalid = FakeEvent("99")
    await dispatch(invalid)
    assert "1 到 1" in invalid.sent[0].get_plain_text()
    assert not first.done()
    cancel = FakeEvent("取消")
    await dispatch(cancel)
    assert await first == (None, cancel)
    assert not ui._pending


@pytest.mark.asyncio
async def test_timeout_and_plugin_unload_remove_sessions():
    ui = CoverSelectionUI(buttons=False)
    with pytest.raises(TimeoutError):
        await ui.choose(FakeEvent(), "选歌", ["A"], 1)
    event = FakeEvent()
    task = asyncio.create_task(ui.choose(event, "选歌", ["A"], 30))
    await wait_for_menu(event)
    await ui.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not ui._pending


@pytest.mark.asyncio
async def test_total_send_failure_removes_session():
    event = FakeEvent()
    event.send = AsyncMock(side_effect=RuntimeError("offline"))
    ui = CoverSelectionUI(buttons=False)
    with pytest.raises(RuntimeError, match="offline"):
        await ui.choose(event, "选歌", ["A"], 30)
    assert not ui._pending


@pytest.mark.asyncio
async def test_unload_cancels_an_inflight_menu_send():
    event = FakeEvent()
    entered = asyncio.Event()

    async def pending_send(**_kwargs):
        entered.set()
        await asyncio.Event().wait()

    event.bot.api.post_group_message.side_effect = pending_send
    ui = CoverSelectionUI()
    task = asyncio.create_task(ui.choose(event, "选歌", ["A"], 30))
    await entered.wait()
    await ui.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not ui._pending
    assert not event.sent


@pytest.mark.asyncio
async def test_real_sdk_preserves_keyboard_envelope():
    from botpy.api import BotAPI

    event = FakeEvent(scene="c2c")
    http = SimpleNamespace(request=AsyncMock(return_value={"id": "sdk-response"}))
    event.bot.api = BotAPI(http)
    await _Menu("选歌", ["A"], 30, "token", True).send(event)
    payload = http.request.await_args.kwargs["json"]
    assert payload["msg_type"] == 2
    assert (
        payload["keyboard"]["content"]["rows"][0]["buttons"][0]["action"]["data"]
        == "matsuko-cover:token:1"
    )
    assert payload["msg_id"] == "input-1"
    assert not event.sent


@pytest.mark.asyncio
async def test_real_guild_dm_with_channel_id_uses_dm_route():
    from botpy.message import DirectMessage

    event = FakeEvent()
    source = DirectMessage.__new__(DirectMessage)
    source.guild_id = "guild-id"
    source.channel_id = "dm-channel-id"
    event.message_obj.raw_message = source
    await _Menu("选歌", ["A"], 30, "token", True).send(event)
    event.bot.api.post_dms.assert_awaited_once()
    event.bot.api.post_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["#", "!", "喵", ""])
async def test_buttons_follow_the_conversation_wake_prefix(prefix):
    event = FakeEvent()
    origins = []

    def get_config(*, umo):
        origins.append(umo)
        return {"wake_prefix": [prefix]}

    ui = CoverSelectionUI(config_getter=get_config)
    task = asyncio.create_task(ui.choose(event, "选歌", ["A"], 30))
    await wait_for_menu(event)
    payload = event.bot.api.post_group_message.await_args.kwargs
    command = payload["keyboard"]["content"]["rows"][0]["buttons"][0]["action"]["data"]
    assert command.startswith("matsuko-cover:")
    reply = FakeEvent(prefix + "1")
    await dispatch(reply)
    assert await task == (0, reply)
    assert origins == [event.unified_msg_origin]
