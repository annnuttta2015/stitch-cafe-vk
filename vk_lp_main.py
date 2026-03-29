"""
VK bot implementation for Stitch Cafe using raw Long Poll API.

This version does NOT depend on vkbottle. It:
 - uses groups.getLongPollServer to receive events
 - handles text commands: /start, /new, /my, /done
 - reuses the same database and game logic as the Telegram bot
"""

from __future__ import annotations

import json
import random
import time
from typing import Any

import requests
from loguru import logger

from config import VK_ALLOWED_PEER_ID, VK_GROUP_ID, VK_TOKEN
from data.dishes import DISHES_BY_LEVEL
from data.levels import LEVELS
from data.special_orders import check_special_order
from data.texts import (
    ADMIN_ONLY,
    ALREADY_HAS_ORDER,
    DISH_LINE,
    DONE_ORDER,
    DONE_WITH_LEVEL_UP,
    EMPTY_DB,
    GAME_COMPLETE,
    HELLO,
    LEVEL_FALLBACK,
    NEW_ORDER_MESSAGE,
    NO_ACTIVE_ORDER,
    NO_PLAYERS_IN_RATING,
    ORDER_TOTAL,
    SELECT_ACTION,
    STATS_HEADER,
    STATS_LINE,
    TOP10_HEADER,
    TOP10_LINE,
    TROPHY_DIAMOND,
    TROPHY_GOLD,
    RESET_SUCCESS,
)
from database import (
    clear_active_order,
    fetch_user,
    finish_order_and_level,
    get_active_order,
    get_db,
    get_last_order,
    save_active_order,
)
from vk_utils import format_vk_user_mention
from utils import is_admin


VK_API_URL = "https://api.vk.com/method/"
VK_API_VERSION = "5.199"


if not VK_TOKEN:
    logger.error("VK_TOKEN is not set. Fill in .env")
    raise RuntimeError("VK_TOKEN is not set. Fill in .env")

try:
    GROUP_ID_INT = int(VK_GROUP_ID)
except (TypeError, ValueError):
    logger.error("VK_GROUP_ID is not set or invalid. Put numeric group id (without minus) in .env")
    raise

try:
    ALLOWED_PEER_ID_INT = int(VK_ALLOWED_PEER_ID) if VK_ALLOWED_PEER_ID else None
except (TypeError, ValueError):
    ALLOWED_PEER_ID_INT = None


def vk_api_call(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Call VK API method using requests."""
    payload = {
        "access_token": VK_TOKEN,
        "v": VK_API_VERSION,
        **params,
    }
    resp = requests.post(VK_API_URL + method, data=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        err = data["error"]
        raise RuntimeError(f"VK API error {err.get('error_code')}: {err.get('error_msg')}")
    return data["response"]


def get_longpoll_server() -> tuple[str, str, str]:
    """Get Long Poll server, key and ts for the group."""
    resp = vk_api_call(
        "groups.getLongPollServer",
        {
            "group_id": GROUP_ID_INT,
        },
    )
    server: str = resp["server"]
    key: str = resp["key"]
    ts: str = resp["ts"]
    logger.info("Obtained Long Poll server")
    return server, key, ts


def build_main_keyboard() -> str:
    """Build persistent main keyboard with three buttons."""
    keyboard = {
        "one_time": False,
        "buttons": [
            [
                {
                    "action": {
                        "type": "text",
                        "label": "🧾 Новый заказ",
                        "payload": json.dumps({"cmd": "new"}, ensure_ascii=False),
                    },
                    "color": "primary",
                },
            ],
            [
                {
                    "action": {
                        "type": "text",
                        "label": "📋 Мой заказ",
                        "payload": json.dumps({"cmd": "my"}, ensure_ascii=False),
                    },
                    "color": "secondary",
                },
                {
                    "action": {
                        "type": "text",
                        "label": "✅ Готово",
                        "payload": json.dumps({"cmd": "done"}, ensure_ascii=False),
                    },
                    "color": "positive",
                },
            ],
        ],
    }
    return json.dumps(keyboard, ensure_ascii=False)


def send_message(peer_id: int, text: str, keyboard: str | None = None) -> None:
    """Send plain text message (optionally with keyboard) to peer_id via messages.send."""
    try:
        params: dict[str, Any] = {
            "peer_id": peer_id,
            "random_id": random.randint(1, 2**31 - 1),
            "message": text,
        }
        if keyboard is not None:
            params["keyboard"] = keyboard
        vk_api_call("messages.send", params)
    except Exception as e:
        logger.error(f"Failed to send message to {peer_id}: {e}")


def get_user_first_name(user_id: int) -> str:
    """Fetch user's first name via users.get."""
    try:
        resp = vk_api_call(
            "users.get",
            {
                "user_ids": user_id,
                "fields": "first_name",
            },
        )
        if resp and isinstance(resp, list):
            return resp[0].get("first_name") or "Гость"
    except Exception as e:
        logger.error(f"Failed to get user info for {user_id}: {e}")
    return "Гость"


def _order_index(total_orders: int) -> int:
    """Compute the next order number."""
    return (total_orders or 0) + 1


async def generate_regular_order(level: int) -> list[tuple[str, int]]:
    """
    Generate a regular order of 3 dishes.

    Logic is copied from commands/order.py.
    """
    dish_level = min(level, 3)
    opened: list[tuple[str, int]] = []
    for lv in range(0, dish_level + 1):
        opened.extend(DISHES_BY_LEVEL.get(lv, []))
    current_pool = DISHES_BY_LEVEL.get(dish_level, DISHES_BY_LEVEL[0])
    cur = random.choice(current_pool)
    pool = [d for d in opened if d != cur]
    random.shuffle(pool)
    take: list[tuple[str, int]] = [cur]
    for d in pool:
        if d not in take and len(take) < 3:
            take.append(d)
    while len(take) < 3:
        for d in DISHES_BY_LEVEL[0]:
            if d not in take:
                take.append(d)
            if len(take) == 3:
                break
    return take[:3]


async def handle_start(user_id: int, peer_id: int) -> None:
    """Handle /start command for VK."""
    first_name = get_user_first_name(user_id)
    async with get_db() as db:
        user = await fetch_user(db, user_id, first_name)
    name_mention = format_vk_user_mention(user_id, user.get("first_name") or first_name)
    kb = build_main_keyboard()
    send_message(
        peer_id,
        HELLO.format(name=name_mention),
        keyboard=kb,
    )
    send_message(
        peer_id,
        "Команды:\n/new — новый заказ\n/my — мой заказ\n/done — отметить заказ как готовый",
        keyboard=kb,
    )
    send_message(peer_id, SELECT_ACTION, keyboard=kb)


async def _vk_new_order_logic(user: dict[str, Any], user_id: int, first_name: str) -> str:
    """Shared logic for creating a new order. Returns text."""
    idx = _order_index(user["total_orders"])

    last_order_was_special = False
    if user["total_orders"] > 0:
        async with get_db() as db:
            last_order = await get_last_order(db, user["user_id"])
        if last_order and last_order.get("tag"):
            last_order_was_special = True

    async with get_db() as db:
        if not last_order_was_special:
            user_flags = {
                "has_student_done": user.get("has_student_done", 0),
                "has_critic_done": user.get("has_critic_done", 0),
                "has_dirty_plate_done": user.get("has_dirty_plate_done", 0),
                "has_second_chef_done": user.get("has_second_chef_done", 0),
            }
            special_result = check_special_order(idx, user_flags)
        else:
            special_result = None

        name_mention = format_vk_user_mention(user_id, user.get("first_name") or first_name)

        if special_result:
            tag, order_config = special_result
            order_type = order_config.get("type", "regular")

            if order_type == "double_previous":
                last_order = await get_last_order(db, user["user_id"])
                if last_order:
                    last_dishes = last_order.get("dishes", [])
                    last_crosses = last_order.get("crosses", 0)
                    doubled_dishes = [(name, crosses * 2) for name, crosses in last_dishes]
                    doubled_crosses = last_crosses * 2
                    text = order_config["text_template"].format(
                        name=name_mention,
                        doubled_crosses=doubled_crosses,
                    )
                    await save_active_order(db, user["user_id"], doubled_dishes, tag)
                    return text
            elif order_type == "half_new_order":
                level = user["level"]
                dishes = await generate_regular_order(level)
                total = sum(c for (_, c) in dishes)
                half_total = total // 2
                half_dishes = [(name, max(1, crosses // 2)) for name, crosses in dishes]
                half_crosses = sum(v for (_, v) in half_dishes)
                if half_dishes and half_crosses != half_total:
                    diff = half_total - half_crosses
                    name0, val0 = half_dishes[0]
                    half_dishes[0] = (name0, max(1, val0 + diff))
                    half_crosses = half_total
                lines = "\n".join(
                    [DISH_LINE.format(name=n, crosses=v) for (n, v) in half_dishes]
                )
                text = order_config["text_template"].format(
                    name=name_mention,
                    half_crosses=half_crosses,
                    dishes=lines,
                )
                await save_active_order(db, user["user_id"], half_dishes, tag)
                return text
            elif order_type == "regular":
                dishes = [order_config["dish"]]
                text = order_config["text_template"].format(name=name_mention)
                await save_active_order(db, user["user_id"], dishes, tag)
                return text

        level = user["level"]
        dishes = await generate_regular_order(level)
        total = sum(x[1] for x in dishes)
        lines = "\n".join([DISH_LINE.format(name=n, crosses=v) for (n, v) in dishes])
        order_number = _order_index(user["total_orders"])
        text = (
            NEW_ORDER_MESSAGE.format(
                name=name_mention,
                order_number=order_number,
                dishes=lines,
            )
            + ORDER_TOTAL.format(total=total)
        )
        await save_active_order(db, user["user_id"], dishes, None)
        return text


async def handle_new(user_id: int, peer_id: int) -> None:
    """Handle /new command."""
    first_name = get_user_first_name(user_id)
    async with get_db() as db:
        user = await fetch_user(db, user_id, first_name)
        active = await get_active_order(db, user["user_id"])
        if active is not None:
            name_mention = format_vk_user_mention(
                user_id,
                user.get("first_name") or first_name,
            )
            send_message(
                peer_id,
                ALREADY_HAS_ORDER.format(name=name_mention),
                keyboard=build_main_keyboard(),
            )
            return

    text = await _vk_new_order_logic(user, user_id, first_name)
    send_message(peer_id, text, keyboard=build_main_keyboard())


async def handle_my(user_id: int, peer_id: int) -> None:
    """Handle /my command."""
    first_name = get_user_first_name(user_id)
    async with get_db() as db:
        user = await fetch_user(db, user_id, first_name)
        active = await get_active_order(db, user["user_id"])
        if not active:
            name_mention = format_vk_user_mention(
                user_id,
                user.get("first_name") or first_name,
            )
            send_message(
                peer_id,
                NO_ACTIVE_ORDER.format(name=name_mention),
                keyboard=build_main_keyboard(),
            )
            return
        dishes = active["dishes"]
        lines = "\n".join([DISH_LINE.format(name=n, crosses=v) for (n, v) in dishes])
        total = sum(v for (_, v) in dishes)
        name_mention = format_vk_user_mention(
            user_id,
            user.get("first_name") or first_name,
        )
        text = (
            f"👩‍🍳 {name_mention}, твой текущий заказ:\n\n{lines}"
            + ORDER_TOTAL.format(total=total)
        )
        send_message(peer_id, text, keyboard=build_main_keyboard())


async def handle_done(user_id: int, peer_id: int) -> None:
    """Handle /done command."""
    first_name = get_user_first_name(user_id)
    async with get_db() as db:
        user = await fetch_user(db, user_id, first_name)
        active = await get_active_order(db, user["user_id"])
        if not active:
            name_mention = format_vk_user_mention(
                user_id,
                user.get("first_name") or first_name,
            )
            send_message(
                peer_id,
                NO_ACTIVE_ORDER.format(name=name_mention),
                keyboard=build_main_keyboard(),
            )
            return

        order_crosses = sum(v for (_, v) in active["dishes"])
        (
            n_total,
            level_changed,
            new_title,
            total_crosses,
        ) = await finish_order_and_level(
            db,
            user["user_id"],
            active.get("tag"),
            order_crosses,
        )
        await clear_active_order(db, user["user_id"])

    name_mention = format_vk_user_mention(user_id, first_name)
    if level_changed:
        txt = DONE_WITH_LEVEL_UP.format(
            name=name_mention,
            n=n_total,
            title=new_title,
            total_crosses=total_crosses,
        )
    else:
        txt = DONE_ORDER.format(
            name=name_mention,
            n=n_total,
            total_crosses=total_crosses,
            title=new_title,
        )

    if n_total == 40:
        txt += GAME_COMPLETE
    elif n_total == 100:
        txt += TROPHY_GOLD
    elif n_total == 200:
        txt += TROPHY_DIAMOND

    send_message(peer_id, txt, keyboard=build_main_keyboard())


async def handle_top(user_id: int, peer_id: int) -> None:
    """Handle /top command (admins only): full stats in chat."""
    if not is_admin(str(user_id)):
        name_mention = format_vk_user_mention(user_id, get_user_first_name(user_id))
        send_message(peer_id, ADMIN_ONLY.format(name=name_mention))
        return

    async with get_db() as db:
        cur = await db.execute(
            """
            SELECT first_name, level, total_orders, has_student_done, has_critic_done,
                   has_dirty_plate_done, has_second_chef_done
            FROM users
            ORDER BY total_orders DESC, level DESC
            """
        )
        rows = await cur.fetchall()

    if not rows:
        send_message(peer_id, EMPTY_DB)
        return

    lines = [STATS_HEADER]
    for i, r in enumerate(rows, start=1):
        level_title = LEVELS.get(r["level"], LEVEL_FALLBACK.format(level=r["level"]))
        student = "✅" if r["has_student_done"] else "❌"
        critic = "✅" if r["has_critic_done"] else "❌"
        dirty = "✅" if r["has_dirty_plate_done"] else "❌"
        chef = "✅" if r["has_second_chef_done"] else "❌"
        lines.append(
            STATS_LINE.format(
                num=i,
                name=r["first_name"] or "",
                orders=r["total_orders"],
                level=level_title,
                student=student,
                critic=critic,
                dirty=dirty,
                chef=chef,
            )
        )
    send_message(peer_id, "\n".join(lines))


async def handle_top10(user_id: int, peer_id: int) -> None:
    """Handle /top10 command (admins only): top-10 in chat."""
    if not is_admin(str(user_id)):
        name_mention = format_vk_user_mention(user_id, get_user_first_name(user_id))
        send_message(peer_id, ADMIN_ONLY.format(name=name_mention))
        return

    async with get_db() as db:
        cur = await db.execute(
            """
            SELECT user_id, first_name, level, total_orders
            FROM users
            ORDER BY total_orders DESC, level DESC
            LIMIT 10
            """
        )
        rows = await cur.fetchall()

    if not rows:
        send_message(peer_id, NO_PLAYERS_IN_RATING)
        return

    lines = [TOP10_HEADER]
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

    for i, r in enumerate(rows):
        level_title = LEVELS.get(r["level"], LEVEL_FALLBACK.format(level=r["level"]))
        medal = medals[i] if i < len(medals) else f"{i + 1}."
        name_mention = format_vk_user_mention(r["user_id"], r["first_name"] or "")
        lines.append(
            TOP10_LINE.format(
                medal=medal,
                name=name_mention,
                orders=r["total_orders"],
                level=level_title,
            )
        )

    send_message(peer_id, "\n".join(lines))


async def handle_reset(user_id: int, peer_id: int) -> None:
    """Handle /reset command (admins only): wipe DB."""
    if not is_admin(str(user_id)):
        name_mention = format_vk_user_mention(user_id, get_user_first_name(user_id))
        send_message(peer_id, ADMIN_ONLY.format(name=name_mention))
        return

    async with get_db() as db:
        await db.execute("DELETE FROM users")
        await db.commit()

    name_mention = format_vk_user_mention(user_id, get_user_first_name(user_id))
    send_message(peer_id, RESET_SUCCESS.format(name=name_mention))


# Long Poll: меньше wait = чаще возвращаемся из запроса, не «залипаем» на плохом интернете
LONGPOLL_WAIT = 15
LONGPOLL_TIMEOUT = 22  # чуть больше wait, чтобы не обрывать нормальный ответ
HEARTBEAT_EVERY = 20   # раз в N циклов пишем в лог, что бот жив


def longpoll_loop() -> None:
    """Main blocking Long Poll loop."""
    logger.info("Starting VK Long Poll loop...")
    server, key, ts = get_longpoll_server()
    cycle = 0

    while True:
        try:
            url = f"{server}?act=a_check&key={key}&ts={ts}&wait={LONGPOLL_WAIT}"
            resp = requests.get(url, timeout=LONGPOLL_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, json.JSONDecodeError) as e:
            logger.warning(f"Long Poll запрос оборвался или ошибка: {e}. Переподключаюсь через 3 сек...")
            time.sleep(3)
            try:
                server, key, ts = get_longpoll_server()
            except Exception as e2:
                logger.error(f"Не удалось получить Long Poll сервер: {e2}")
                time.sleep(5)
            continue

        cycle += 1
        if cycle % HEARTBEAT_EVERY == 0:
            logger.info(f"Long poll жив, цикл {cycle} (каждые ~{HEARTBEAT_EVERY * LONGPOLL_WAIT} сек)")

        if "failed" in data:
            # According to VK docs, need to refresh key/ts
            failed = data["failed"]
            logger.warning(f"Long Poll failed code: {failed}")
            if failed in (1,):
                ts = data.get("ts", ts)
            else:
                server, key, ts = get_longpoll_server()
            continue

        ts = data.get("ts", ts)
        updates = data.get("updates", [])

        for upd in updates:
            if upd.get("type") != "message_new":
                continue
            obj = upd.get("object", {})
            msg = obj.get("message", {})
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            from_id = msg.get("from_id")
            peer_id = msg.get("peer_id")
            if not isinstance(from_id, int) or not isinstance(peer_id, int):
                continue

            # Ограничение на одну беседу, если настроено
            if ALLOWED_PEER_ID_INT is not None and peer_id != ALLOWED_PEER_ID_INT:
                continue

            logger.info(f"New message: from_id={from_id}, peer_id={peer_id}, text={text!r}")

            # Routing by simple commands or keyboard payload
            payload_raw = msg.get("payload")
            cmd: str | None = None
            if isinstance(payload_raw, str):
                try:
                    payload_obj = json.loads(payload_raw)
                    if isinstance(payload_obj, dict):
                        cmd = str(payload_obj.get("cmd") or "").lower()
                except json.JSONDecodeError:
                    cmd = None

            lower = text.lower()
            if cmd == "new" or lower.startswith("/new"):
                import asyncio

                asyncio.run(handle_new(from_id, peer_id))
            elif cmd == "my" or lower.startswith("/my"):
                import asyncio

                asyncio.run(handle_my(from_id, peer_id))
            elif cmd == "done" or lower.startswith("/done"):
                import asyncio

                asyncio.run(handle_done(from_id, peer_id))
            elif lower.startswith("/start"):
                import asyncio

                asyncio.run(handle_start(from_id, peer_id))
            elif lower.startswith("/top10"):
                import asyncio

                asyncio.run(handle_top10(from_id, peer_id))
            elif lower.startswith("/top"):
                import asyncio

                asyncio.run(handle_top(from_id, peer_id))
            elif lower.startswith("/reset"):
                import asyncio

                asyncio.run(handle_reset(from_id, peer_id))
            elif lower.startswith("/"):
                # неизвестная /команда – можно ответить, но не спамить на обычный текст
                send_message(
                    peer_id,
                    "Неизвестная команда. Доступны: /start, /new, /my, /done, /top, /top10, /reset.",
                    keyboard=build_main_keyboard(),
                )
            else:
                # обычный текст – игнорируем, чтобы бот не мешал общению
                continue


if __name__ == "__main__":
    longpoll_loop()

