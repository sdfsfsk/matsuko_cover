"""Cover menus with native QQ callback buttons and text-selection fallback."""

import asyncio
import re
import secrets

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Node, Plain
from astrbot.core.utils.session_waiter import (
    FILTERS,
    SessionController,
    SessionFilter,
    SessionWaiter,
)

from .qq_callbacks import (
    CALLBACK_PREFIX,
    OFFICIAL_PLATFORMS,
    CallbackTarget,
    QQCallbackBridge,
)

PAGE_SIZE = 8
CHOICE_COMMAND = "翻唱选择"


class _ChoiceFilter(SessionFilter):
    """Only route selection replies from the menu owner in the same conversation."""

    def __init__(self, event: AstrMessageEvent, token: str, key: str, prefix: str):
        self.origin = event.unified_msg_origin
        self.sender = str(event.get_sender_id())
        self.token = token
        self.key = key
        self.prefix = prefix

    def parse(self, event: AstrMessageEvent) -> str | None:
        """Parse text or a command button, rejecting a different menu nonce.

        Args:
            event: Incoming reply to inspect.

        Returns:
            A selection action, or None for unrelated input.
        """
        if (
            event.unified_msg_origin != self.origin
            or str(event.get_sender_id()) != self.sender
        ):
            return None
        text = str(event.message_str or "").strip()
        # Official group/channel messages can retain the leading bot mention.
        text = re.sub(r"^(?:<@!?[^>]+>\s*)+", "", text).strip()
        if self.prefix and text.startswith(self.prefix):
            text = text[len(self.prefix) :].strip()
        text = text.lstrip("/#！!").strip()
        if text.startswith(CHOICE_COMMAND):
            parts = text.split()
            if len(parts) != 3 or parts[1] != self.token:
                return None
            text = parts[2]
        aliases = {
            "取消": "cancel",
            "取消选择": "cancel",
            "上一页": "prev",
            "上页": "prev",
            "下一页": "next",
            "下页": "next",
        }
        text = aliases.get(text, text.lower())
        if text in ("prev", "next", "cancel") or re.fullmatch(r"[0-9]{1,6}", text):
            return text
        return None

    def filter(self, event: AstrMessageEvent) -> str:
        """Return this session only for a matching selection reply.

        Args:
            event: Incoming message.

        Returns:
            The active session key or an unmatched empty key.
        """
        return self.key if self.parse(event) is not None else ""


class _Menu:
    def __init__(
        self,
        title: str,
        choices: list[str],
        timeout: int,
        token: str,
        buttons: bool,
        prefix: str = "/",
    ):
        self.title = str(title)[:300]
        self.choices = [" ".join(str(choice).split())[:200] for choice in choices]
        self.timeout = timeout
        self.token = token
        self.buttons = buttons
        self.prefix = prefix
        self.page = 0
        self.pages = (len(choices) + PAGE_SIZE - 1) // PAGE_SIZE
        self.notice = ""

    async def send(self, event: AstrMessageEvent) -> None:
        """Send a complete page, falling back to text if native buttons fail.

        Args:
            event: Latest user message, supplying a fresh passive-reply ID.

        Raises:
            Exception: If the fallback message itself cannot be delivered.
        """
        start = self.page * PAGE_SIZE
        visible = list(enumerate(self.choices[start : start + PAGE_SIZE], start + 1))
        text = self.title + "\n\n" + "\n".join(f"{i}. {label}" for i, label in visible)
        text += f"\n\n第 {self.page + 1}/{self.pages} 页 · 共 {len(self.choices)} 项"
        text += f"\n请在 {self.timeout} 秒内选择，或发送序号；发送“取消”结束。"
        if self.pages > 1:
            text += "\n发送“上一页”或“下一页”翻页。"
        if self.notice:
            text += "\n" + self.notice

        official = event.get_platform_name() in OFFICIAL_PLATFORMS
        if official and self.buttons:
            try:
                await self._send_buttons(event, text, visible)
                return
            except Exception as exc:
                logger.warning(
                    "[MatsukoCover] QQ button menu failed: %s", type(exc).__name__
                )
                self.buttons = False
                text += "\n按钮暂不可用，请直接发送序号选择。"
        if event.get_platform_name() == "aiocqhttp":
            await event.send(
                event.chain_result(
                    [Node(uin=1109587454, name="松子", content=[Plain(text)])]
                )
            )
        else:
            await event.send(event.plain_result(text).use_markdown(False))

    async def _send_buttons(
        self, event: AstrMessageEvent, text: str, visible: list[tuple[int, str]]
    ) -> None:
        """Use QQ's callback-button protocol for groups, C2C and guild messages.

        Args:
            event: QQ Official event holding its authenticated SDK client.
            text: Menu text, sent in the same message as the keyboard.
            visible: Absolute choice numbers and labels for this page.

        Raises:
            RuntimeError: If the SDK does not return a successful message.
            ValueError: If this QQ event has no supported message destination.
        """
        source = event.message_obj.raw_message
        api = event.bot.api
        if getattr(source, "group_openid", None):
            send = api.post_group_message
            destination = {"group_openid": source.group_openid, "msg_type": 2}
        elif getattr(getattr(source, "author", None), "user_openid", None):
            send = api.post_c2c_message
            destination = {"openid": source.author.user_openid, "msg_type": 2}
        elif getattr(source, "guild_id", None) and not getattr(
            source, "channel_id", None
        ):
            send = api.post_dms
            destination = {"guild_id": source.guild_id}
        elif getattr(source, "channel_id", None):
            # Guild DMs carry both guild_id and channel_id in the SDK.
            from botpy.message import DirectMessage

            if isinstance(source, DirectMessage):
                send = api.post_dms
                destination = {"guild_id": source.guild_id}
            else:
                send = api.post_message
                destination = {"channel_id": source.channel_id}
        else:
            raise ValueError("Unsupported QQ message destination")

        buttons = []
        actions = [(str(i), f"{i}. {label}"[:10]) for i, label in visible]
        for action, label in actions:
            buttons.append(
                {
                    "id": f"pick_{action}",
                    "render_data": {
                        "label": label,
                        "visited_label": "已选择",
                        "style": 1,
                    },
                    "action": {
                        "type": 1,
                        "permission": {
                            "type": 0,
                            "specify_user_ids": [str(event.get_sender_id())],
                        },
                        "data": f"{CALLBACK_PREFIX}{self.token}:{action}",
                        "unsupport_tips": "请发送对应序号选择",
                    },
                }
            )
        rows = [{"buttons": buttons[i : i + 2]} for i in range(0, len(buttons), 2)]
        navigation = []
        if self.page > 0:
            navigation.append(("prev", "上一页"))
        if self.page + 1 < self.pages:
            navigation.append(("next", "下一页"))
        navigation.append(("cancel", "取消"))
        controls = []
        for action, label in navigation:
            controls.append(
                {
                    "id": action,
                    "render_data": {"label": label, "visited_label": label, "style": 0},
                    "action": {
                        "type": 1,
                        "permission": {
                            "type": 0,
                            "specify_user_ids": [str(event.get_sender_id())],
                        },
                        "data": f"{CALLBACK_PREFIX}{self.token}:{action}",
                        "unsupport_tips": f"请发送“{label}”",
                    },
                }
            )
        rows.append({"buttons": controls})
        text += "\n点击按钮即可选择，无需再发送确认消息。"
        escaped = re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", text)
        payload = {
            **destination,
            "msg_id": event.message_obj.message_id,
            "markdown": {"content": escaped},
            "keyboard": {"content": {"rows": rows}},
        }
        if "msg_type" in destination:
            payload["msg_seq"] = secrets.randbelow(900_000) + 10_001
        result = await asyncio.wait_for(send(**payload), timeout=15)
        if result is None or (
            isinstance(result, dict) and (result.get("code") or not result.get("id"))
        ):
            raise RuntimeError("QQ button message was not acknowledged")
        event._has_send_oper = True


class CoverSelectionUI:
    """Own pending menus so reload and cancellation release their waiters."""

    def __init__(self, buttons: bool = True, config_getter=None):
        self.buttons = buttons
        self.config_getter = config_getter
        self._pending: dict[str, asyncio.Task] = {}
        self.callbacks = QQCallbackBridge()

    def bind_platforms(self, platforms) -> None:
        """Attach handlers when QQ platforms load or the plugin reloads.

        Args:
            platforms: AstrBot's current platform instances.
        """
        if self.buttons:
            for platform in platforms:
                name = platform.meta().name
                if name in OFFICIAL_PLATFORMS:
                    self.callbacks.bind(platform.get_client(), name)

    async def choose(
        self, event: AstrMessageEvent, title: str, choices: list[str], timeout: int
    ) -> tuple[int | None, AstrMessageEvent]:
        """Display a menu and return a zero-based selection plus its reply event.

        Args:
            event: Message opening the menu.
            title: Menu heading.
            choices: Nonempty display labels in their original selection order.
            timeout: Allowed seconds after each displayed page.

        Returns:
            The selected index (None on cancel) and the latest user reply event.

        Raises:
            TimeoutError: If the user does not select before the deadline.
            ValueError: If choices are empty or this user already has a menu.
        """
        if not choices:
            raise ValueError("没有可供选择的项目。")
        key = f"matsuko_cover:{event.unified_msg_origin}:{event.get_sender_id()}"
        if key in self._pending:
            raise ValueError("你已有正在等待选择的翻唱菜单，请先完成选择或发送“取消”。")
        token = secrets.token_hex(6)
        config = (
            self.config_getter(umo=event.unified_msg_origin)
            if self.config_getter
            else {}
        )
        prefixes = config.get("wake_prefix", ["/"])
        prefix = next((value for value in prefixes if isinstance(value, str)), "/")
        menu = _Menu(title, choices, max(1, int(timeout)), token, self.buttons, prefix)
        if self.buttons and event.get_platform_name() in OFFICIAL_PLATFORMS:
            if not self.callbacks.bind(event.bot, event.get_platform_name()):
                menu.buttons = False
                menu.notice = (
                    "回调订阅待生效，请重启 QQ 官方机器人平台连接。当前可发送序号选择。"
                )
        session_filter = _ChoiceFilter(event, token, key, prefix)
        waiter = SessionWaiter(session_filter, key, record_history_chains=False)
        selected = None
        reply_event = event

        async def on_choice(controller: SessionController, reply: AstrMessageEvent):
            nonlocal selected, reply_event
            action = session_filter.parse(reply)
            if action is None:
                return
            reply_event = reply
            if action == "cancel":
                controller.stop()
            elif action in ("prev", "next"):
                page = menu.page + (1 if action == "next" else -1)
                if 0 <= page < menu.pages:
                    menu.page = page
                    await menu.send(reply)
                    controller.keep(menu.timeout, reset_timeout=True)
                else:
                    await reply.send(
                        reply.plain_result(
                            "已经是第一页或最后一页，请选择本页项目。"
                        ).use_markdown(False)
                    )
            elif 1 <= int(action) <= len(choices):
                selected = int(action) - 1
                controller.stop()
            else:
                await reply.send(
                    reply.plain_result(
                        f"请输入 1 到 {len(choices)} 之间的序号，或发送“取消”。"
                    ).use_markdown(False)
                )

        async def on_callback(action, reply):
            reply.message_str = f"{prefix}{CHOICE_COMMAND} {token} {action}"
            await SessionWaiter.trigger(key, reply)

        if menu.buttons and event.get_platform_name() in OFFICIAL_PLATFORMS:
            self.callbacks.targets[token] = CallbackTarget(
                event=event,
                handler=on_callback,
                accepts=lambda action: (
                    action == "cancel"
                    or (action == "prev" and menu.page > 0)
                    or (action == "next" and menu.page + 1 < menu.pages)
                    or (action.isdecimal() and 1 <= int(action) <= len(choices))
                ),
                finished=waiter.session_controller.future.done,
            )

        FILTERS.append(session_filter)
        task = asyncio.create_task(waiter.register_wait(on_choice, menu.timeout))
        owner_task = asyncio.current_task()
        assert owner_task is not None
        self._pending[key] = owner_task
        try:
            # register_wait installs the session before its first suspension.
            await asyncio.sleep(0)
            await menu.send(event)
            if not waiter.session_controller.future.done():
                waiter.session_controller.keep(menu.timeout, reset_timeout=True)
            await task
            return selected, reply_event
        finally:
            self.callbacks.targets.pop(token, None)
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # Cancellation can arrive before register_wait starts its cleanup.
            if session_filter in FILTERS:
                FILTERS.remove(session_filter)
            self._pending.pop(key, None)

    async def send_list(self, event: AstrMessageEvent, text: str) -> None:
        """Render model listings without unsupported forwarded nodes on QQ Official.

        Args:
            event: Destination message event.
            text: Complete model listing to display.
        """
        if event.get_platform_name() == "aiocqhttp":
            await event.send(
                event.chain_result(
                    [Node(uin=1109587454, name="松子", content=[Plain(text)])]
                )
            )
            return
        for offset in range(0, len(text), 3000):
            await event.send(
                event.plain_result(text[offset : offset + 3000]).use_markdown(False)
            )

    async def close(self) -> None:
        """Cancel pending menus, including sends, and await waiter cleanup."""
        tasks = list(self._pending.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.callbacks.close()
