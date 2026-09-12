"""QQ callback-button routing and event-scoped passive reply credentials."""

import asyncio
import copy
import inspect
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

INTERACTION_INTENT = 1 << 26
CALLBACK_PREFIX = "matsuko-cover:"
OFFICIAL_PLATFORMS = {"qq_official", "qq_official_webhook"}
_MISSING = object()


def original_client(client):
    """Return the SDK client behind a callback-specific reply proxy."""
    return getattr(client, "_cover_original_client", client)


class _CallbackHttp:
    def __init__(self, http, event_id: str):
        self._http = http
        self._event_id = event_id

    def __getattr__(self, name):
        return getattr(self._http, name)

    async def request(self, route, **kwargs):
        """Attach the event ID only to message sends, preserving upload requests."""
        if getattr(route, "method", "").upper() == "POST" and re.fullmatch(
            r"/(?:v2/(?:groups|users)/[^/]+|channels/[^/]+|dms/[^/]+)/messages",
            getattr(route, "path", ""),
        ):
            payload = dict(kwargs.get("json") or {})
            payload.pop("msg_id", None)
            payload["event_id"] = self._event_id
            kwargs["json"] = payload
        return await self._http.request(route, **kwargs)


class _CallbackAPI:
    def __init__(self, api, event_id: str):
        self._api = api
        self._event_id = event_id
        self._http = _CallbackHttp(getattr(api, "_http", None), event_id)

    def __getattr__(self, name):
        method = getattr(self._api, name)
        if name not in {
            "post_group_message",
            "post_c2c_message",
            "post_message",
            "post_dms",
        }:
            return method

        async def send(*args, **kwargs):
            kwargs.pop("msg_id", None)
            kwargs["event_id"] = self._event_id
            if inspect.ismethod(method):
                # The SDK reintroduces msg_id=None via locals(); route its HTTP
                # request through this event's proxy to remove that field too.
                return await method.__func__(self, *args, **kwargs)
            return await method(*args, **kwargs)

        return send


class _CallbackClient:
    def __init__(self, client, event_id: str):
        self._cover_original_client = original_client(client)
        self.api = _CallbackAPI(self._cover_original_client.api, event_id)

    def __getattr__(self, name):
        return getattr(self._cover_original_client, name)


@dataclass
class CallbackTarget:
    event: Any
    handler: Any
    accepts: Any
    finished: Any
    busy: bool = False


class QQCallbackBridge:
    """Bind callbacks to SDK instances without changing AstrBot or third-party files."""

    def __init__(self):
        self.targets: dict[str, CallbackTarget] = {}
        self._bindings: dict[int, tuple] = {}
        self._seen: OrderedDict[tuple[int, str], None] = OrderedDict()
        self._inflight: set[asyncio.Task] = set()

    def bind(self, client, platform_name: str) -> bool:
        """Register an SDK handler and request the interaction intent.

        Args:
            client: Already authenticated or newly created QQ SDK client.
            platform_name: AstrBot platform adapter name.

        Returns:
            Whether this connection is ready for callback-button events.
        """
        client = original_client(client)
        if platform_name not in OFFICIAL_PLATFORMS:
            return False
        if id(client) not in self._bindings:
            previous = getattr(client, "on_interaction_create", _MISSING)
            old_intents = getattr(client, "intents", 0)
            if not isinstance(old_intents, int):
                return False
            ready = (
                platform_name == "qq_official_webhook"
                or (
                    bool(old_intents & INTERACTION_INTENT)
                    and not getattr(client, "_cover_callback_needs_restart", False)
                )
                or getattr(client, "_connection", None) is None
            )
            client.intents = old_intents | INTERACTION_INTENT
            client._cover_callback_needs_restart = not ready

            async def on_interaction(interaction):
                if not await self.handle(client, interaction) and callable(previous):
                    result = previous(interaction)
                    if inspect.isawaitable(result):
                        await result

            client.on_interaction_create = on_interaction
            self._bindings[id(client)] = (client, previous, on_interaction, ready)
            if not ready:
                logger.warning(
                    "[MatsukoCover] Restart the QQ Official platform connection "
                    "to activate the interaction intent."
                )
        binding = self._bindings[id(client)]
        sockets = list(getattr(client, "_active_websockets", ()))
        if sockets:
            ready = all(
                bool(
                    getattr(socket, "_session", {}).get("intent", 0)
                    & INTERACTION_INTENT
                )
                for socket in sockets
            )
            client._cover_callback_needs_restart = not ready
            return ready
        return binding[3]

    async def handle(self, client, interaction) -> bool:
        """Acknowledge owned button clicks before running their selection action.

        Args:
            client: SDK client which received the authenticated platform event.
            interaction: Parsed QQ INTERACTION_CREATE payload.

        Returns:
            Whether this event belongs to the cover plugin's button namespace.
        """
        resolved = getattr(getattr(interaction, "data", None), "resolved", None)
        data = getattr(resolved, "button_data", None)
        if not isinstance(data, str) or not data.startswith(CALLBACK_PREFIX):
            return False
        if str(getattr(interaction, "type", "")) != "11":
            return False
        interaction_id = getattr(interaction, "id", None)
        if not isinstance(interaction_id, str) or not interaction_id:
            return True
        key = (id(client), interaction_id)
        if key in self._seen:
            return True
        self._seen[key] = None
        while len(self._seen) > 2048:
            self._seen.popitem(last=False)

        match = re.fullmatch(
            r"matsuko-cover:([0-9a-f]{12}):([0-9]{1,6}|prev|next|cancel)", data
        )
        target = self.targets.get(match.group(1)) if match else None
        action = match.group(2) if match else ""
        code = 1
        reserved = False
        if target is not None:
            source = target.event.message_obj.raw_message
            sender = str(target.event.get_sender_id())
            if getattr(source, "group_openid", None):
                authorized = (
                    getattr(interaction, "group_openid", None) == source.group_openid
                    and getattr(interaction, "group_member_openid", None) == sender
                )
            elif getattr(getattr(source, "author", None), "user_openid", None):
                authorized = getattr(interaction, "user_openid", None) == sender
            else:
                authorized = (
                    getattr(interaction, "guild_id", None)
                    == getattr(source, "guild_id", None)
                    and getattr(interaction, "channel_id", None)
                    == getattr(source, "channel_id", None)
                    and getattr(resolved, "user_id", None) == sender
                )
            authorized = authorized and original_client(target.event.bot) is client
            if not authorized:
                code = 4
            elif target.finished():
                code = 3
            elif target.busy:
                code = 2
            elif target.accepts(action):
                code = 0
                target.busy = True
                reserved = True

        task = asyncio.current_task()
        if task:
            self._inflight.add(task)
        try:
            # d.id acknowledges the click; the envelope event ID authorizes replies.
            await asyncio.wait_for(
                client.api.on_interaction_result(
                    interaction_id=interaction_id, code=code
                ),
                timeout=2,
            )
            if code == 0:
                reply_id = getattr(interaction, "event_id", None) or interaction_id
                reply = copy.copy(target.event)
                reply.message_obj = copy.copy(target.event.message_obj)
                reply.bot = _CallbackClient(client, reply_id)
                reply._cover_callback_event_id = reply_id
                reply._result = None
                reply._force_stopped = False
                reply._has_send_oper = False
                reply.send_buffer = None
                await target.handler(action, reply)
        except Exception as exc:
            logger.warning("[MatsukoCover] QQ callback failed: %s", type(exc).__name__)
        finally:
            if reserved:
                target.busy = False
            if task:
                self._inflight.discard(task)
        return True

    async def close(self):
        """Restore previous handlers and cancel callback work owned by the plugin."""
        for client, previous, handler, _ in self._bindings.values():
            if getattr(client, "on_interaction_create", None) is handler:
                if previous is _MISSING:
                    delattr(client, "on_interaction_create")
                else:
                    client.on_interaction_create = previous
        current = asyncio.current_task()
        tasks = [task for task in self._inflight if task is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._bindings.clear()
        self.targets.clear()
        self._seen.clear()
