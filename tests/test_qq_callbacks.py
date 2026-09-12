"""Validate click acknowledgement, authorization, deduplication and reply IDs."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from matsuko_cover_under_test.qq_callbacks import (
    INTERACTION_INTENT,
    CallbackTarget,
    QQCallbackBridge,
)
from matsuko_cover_under_test.selection import CoverSelectionUI
from test_selection import FakeEvent, click, dispatch, wait_for_menu

TOKEN = "0123456789ab"
DATA = f"matsuko-cover:{TOKEN}:1"


@pytest.fixture
def target():
    event = FakeEvent()
    bridge = QQCallbackBridge()
    assert bridge.bind(event.bot, "qq_official")
    handler = AsyncMock()
    bridge.targets[TOKEN] = CallbackTarget(
        event=event,
        handler=handler,
        accepts=lambda action: action in {"1", "cancel"},
        finished=lambda: False,
    )
    return bridge, event, handler


@pytest.mark.asyncio
async def test_ack_is_sent_before_processing_and_uses_inner_interaction_id(target):
    bridge, event, handler = target
    order = []

    async def acknowledge(**kwargs):
        order.append(("ack", kwargs))

    async def process(action, reply):
        order.append(("action", action))
        assert reply._cover_callback_event_id == "INTERACTION_CREATE:click-id"
        assert reply is not event
        assert event.message_obj.message_id == "input-1"

    event.bot.api.on_interaction_result.side_effect = acknowledge
    handler.side_effect = process
    await click(event, DATA, interaction_id="click-id")
    assert order == [
        ("ack", {"interaction_id": "click-id", "code": 0}),
        ("action", "1"),
    ]
    await bridge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides", [{"group_member_openid": "stranger"}, {"group_openid": "other-group"}]
)
async def test_unauthorized_click_is_rejected_without_processing(target, overrides):
    bridge, event, handler = target
    await click(event, DATA, **overrides)
    assert event.bot.api.on_interaction_result.await_args.kwargs["code"] == 4
    handler.assert_not_awaited()
    await bridge.close()


@pytest.mark.asyncio
async def test_click_from_another_bot_client_is_rejected(target):
    bridge, event, handler = target
    other = FakeEvent()
    bridge.bind(other.bot, "qq_official")
    await click(other, DATA)
    assert other.bot.api.on_interaction_result.await_args.kwargs["code"] == 4
    handler.assert_not_awaited()
    await bridge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("finished", "data", "code"),
    [
        (True, DATA, 3),
        (False, f"matsuko-cover:{TOKEN}:99", 1),
        (False, "matsuko-cover:ffffffffffff:1", 1),
    ],
)
async def test_finished_expired_and_invalid_choices_do_not_advance(
    target, finished, data, code
):
    bridge, event, handler = target
    bridge.targets[TOKEN].finished = lambda: finished
    await click(event, data)
    assert event.bot.api.on_interaction_result.await_args.kwargs["code"] == code
    handler.assert_not_awaited()
    await bridge.close()


@pytest.mark.asyncio
async def test_duplicate_delivery_is_acknowledged_only_once(target):
    bridge, event, handler = target
    await click(event, DATA, interaction_id="duplicate")
    await click(event, DATA, interaction_id="duplicate")
    event.bot.api.on_interaction_result.assert_awaited_once()
    handler.assert_awaited_once()
    await bridge.close()


@pytest.mark.asyncio
async def test_busy_click_is_rejected_while_first_action_is_running(target):
    bridge, event, handler = target
    entered, release = asyncio.Event(), asyncio.Event()

    async def process(*_):
        entered.set()
        await release.wait()

    handler.side_effect = process
    first = asyncio.create_task(click(event, DATA, interaction_id="first"))
    await entered.wait()
    await click(event, DATA, interaction_id="second")
    assert event.bot.api.on_interaction_result.await_args.kwargs == {
        "interaction_id": "second",
        "code": 2,
    }
    release.set()
    await first
    handler.assert_awaited_once()
    await bridge.close()


@pytest.mark.asyncio
async def test_ack_failure_does_not_start_work_or_leave_menu_busy(target):
    bridge, event, handler = target
    event.bot.api.on_interaction_result.side_effect = TimeoutError("timeout")
    await click(event, DATA)
    handler.assert_not_awaited()
    assert not bridge.targets[TOKEN].busy
    await bridge.close()


@pytest.mark.asyncio
async def test_other_plugins_keep_their_callback_handler():
    event = FakeEvent()
    previous = AsyncMock()
    event.bot.on_interaction_create = previous
    bridge = QQCallbackBridge()
    bridge.bind(event.bot, "qq_official")
    await click(event, "another-plugin:confirm")
    previous.assert_awaited_once()
    event.bot.api.on_interaction_result.assert_not_awaited()
    await bridge.close()
    assert event.bot.on_interaction_create is previous


@pytest.mark.asyncio
async def test_existing_connection_requires_restart_even_after_plugin_reload():
    event = FakeEvent()
    event.bot._connection = object()
    event.bot.intents = 0
    bridge = QQCallbackBridge()
    assert not bridge.bind(event.bot, "qq_official")
    assert event.bot.intents & INTERACTION_INTENT
    await bridge.close()
    replacement = QQCallbackBridge()
    assert not replacement.bind(event.bot, "qq_official")
    await replacement.close()


@pytest.mark.asyncio
async def test_unsubscribed_connection_shows_text_instead_of_dead_buttons():
    event = FakeEvent()
    event.bot._connection = object()
    ui = CoverSelectionUI()
    task = asyncio.create_task(ui.choose(event, "选歌", ["A"], 30))
    await wait_for_menu(event)
    event.bot.api.post_group_message.assert_not_awaited()
    assert "回调订阅待生效" in event.sent[0].get_plain_text()
    reply = FakeEvent("取消")
    await dispatch(reply)
    assert await task == (None, reply)
    await ui.close()


@pytest.mark.asyncio
async def test_webhook_does_not_require_websocket_intent_renegotiation():
    event = FakeEvent(platform="qq_official_webhook")
    event.bot._connection = object()
    bridge = QQCallbackBridge()
    assert bridge.bind(event.bot, "qq_official_webhook")
    await bridge.close()


@pytest.mark.asyncio
async def test_callback_reply_uses_event_id_without_mutating_shared_client():
    from botpy.api import BotAPI
    from botpy.http import Route

    from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
        QQOfficialMessageEvent,
    )

    event = FakeEvent(scene="c2c")
    http = SimpleNamespace(request=AsyncMock(return_value={"id": "outgoing-id"}))
    event.bot.api = BotAPI(http)
    bridge = QQCallbackBridge()
    bridge.bind(event.bot, "qq_official")
    handler = AsyncMock()
    bridge.targets[TOKEN] = CallbackTarget(
        event, handler, lambda _: True, lambda: False
    )
    await click(event, DATA, interaction_id="reply-id")
    reply = handler.await_args.args[1]
    await reply.bot.api.post_c2c_message(
        openid="owner", msg_id="old-id", content="next menu"
    )
    body = http.request.await_args.kwargs["json"]
    assert body["event_id"] == "INTERACTION_CREATE:reply-id"
    assert "msg_id" not in body

    # AstrBot's C2C path makes a direct HTTP request instead of calling the SDK method.
    await QQOfficialMessageEvent.post_c2c_message(
        reply, openid="owner", msg_id="old-id", content="audio result"
    )
    body = http.request.await_args.kwargs["json"]
    assert body["event_id"] == "INTERACTION_CREATE:reply-id" and "msg_id" not in body

    upload = {"file_type": 3, "url": "https://example.test/audio.silk"}
    await reply.bot.api._http.request(
        Route("POST", "/v2/users/{openid}/files", openid="owner"), json=upload
    )
    assert http.request.await_args.kwargs["json"] == upload
    await event.bot.api.post_c2c_message(
        openid="owner", msg_id="original-id", content="unrelated message"
    )
    body = http.request.await_args.kwargs["json"]
    assert body["msg_id"] == "original-id" and not body.get("event_id")
    await bridge.close()
