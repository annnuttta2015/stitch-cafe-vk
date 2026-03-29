"""
Entry point for the VK version of the Stitch Cafe bot.

This module reuses the existing game logic (database, texts, special orders)
and exposes a VK bot based on vkbottle. It supports basic commands:
/start, /new, /my, /done.
"""

from typing import Any

from loguru import logger
from vkbottle import Bot
from vkbottle.bot import Message
from config import VK_TOKEN, VK_GROUP_ID
from data.special_orders import check_special_order
from data.dishes import DISHES_BY_LEVEL
from data.texts import (
    ALREADY_HAS_ORDER,
    DISH_LINE,
    DONE_ORDER,
    DONE_WITH_LEVEL_UP,
    GAME_COMPLETE,
    NEW_ORDER_MESSAGE,
    NO_ACTIVE_ORDER,
    ORDER_TOTAL,
    SELECT_ACTION,
    TROPHY_DIAMOND,
    TROPHY_GOLD,
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


if not VK_TOKEN:
    logger.error("VK_TOKEN is not set. Fill in .env")
    raise RuntimeError("VK_TOKEN is not set. Fill in .env")


bot = Bot(token=VK_TOKEN)

# If VK_GROUP_ID is provided in .env, set it explicitly on the bot
# so that vkbottle does not try to call groups.getById (which may
# return invalid_access_token for some new token types).
_group_id_int: int | None
try:
    _group_id_int = int(VK_GROUP_ID) if VK_GROUP_ID else None
except ValueError:
    _group_id_int = None

if _group_id_int is not None:
    setattr(bot, "group_id", _group_id_int)


def _order_index(total_orders: int) -> int:
    """
    Compute the next order number.

    Args:
        total_orders: Number of completed orders so far

    Returns:
        Next order number (completed + 1)
    """
    return (total_orders or 0) + 1


async def generate_regular_order(level: int) -> list[tuple[str, int]]:
    """
    Generate a regular order of 3 dishes.

    One dish from current level, two from all unlocked levels (0..level), no duplicates.

    Args:
        level: Player's current level

    Returns:
        List of 3 (dish_name, crosses) tuples
    """
    import random

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


async def _get_vk_user(message: Message) -> tuple[int, str]:
    """
    Fetch VK user id and first name for the author of the message.
    """
    user_id = message.from_id
    try:
        users = await bot.api.users.get(user_ids=[user_id])
        first_name = users[0].first_name if users else "Гость"
    except Exception:
        first_name = "Гость"
    return user_id, first_name


@bot.on.message(text="/start")
async def vk_start_handler(message: Message) -> None:
    """
    VK handler for /start command.

    Registers user in database and sends welcome text.
    """
    try:
        user_id, first_name = await _get_vk_user(message)
        async with get_db() as db:
            user = await fetch_user(db, user_id, first_name)

        name_mention = format_vk_user_mention(user_id, user.get("first_name") or first_name)
        await message.answer(
            "Вышивальное кафе во ВКонтакте готово к работе!\n"
            "Команды:\n"
            "/new — новый заказ\n"
            "/my — мой заказ\n"
            "/done — отметить заказ как готовый\n"
        )
        await message.answer(
            NEW_ORDER_MESSAGE.replace("{order_number}", "…").format(
                name=name_mention, dishes=""
            )
        )
        await message.answer(SELECT_ACTION)
    except Exception as e:
        logger.error(f"Error handling VK /start for user: {e}")
        await message.answer("❌ Произошла ошибка при запуске бота. Попробуйте позже.")


async def _vk_new_order_logic(user: dict[str, Any], user_id: int, first_name: str) -> str:
    """
    Shared logic for creating a new order for VK user.

    Returns text to send to the user.
    """
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
                    doubled_dishes = [
                        (name, crosses * 2) for name, crosses in last_dishes
                    ]
                    doubled_crosses = last_crosses * 2
                    text = order_config["text_template"].format(
                        name=name_mention, doubled_crosses=doubled_crosses
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
                    name=name_mention, half_crosses=half_crosses, dishes=lines
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
                name=name_mention, order_number=order_number, dishes=lines
            )
            + ORDER_TOTAL.format(total=total)
        )
        await save_active_order(db, user["user_id"], dishes, None)
        return text


@bot.on.message(text="/new")
async def vk_new_order(message: Message) -> None:
    """
    VK handler for /new command.
    """
    try:
        user_id, first_name = await _get_vk_user(message)
        async with get_db() as db:
            user = await fetch_user(db, user_id, first_name)
            active = await get_active_order(db, user["user_id"])
            if active is not None:
                name_mention = format_vk_user_mention(
                    user_id, user.get("first_name") or first_name
                )
                await message.answer(ALREADY_HAS_ORDER.format(name=name_mention))
                return

        text = await _vk_new_order_logic(user, user_id, first_name)
        await message.answer(text)
    except Exception as e:
        logger.error(f"Error creating VK order for user {message.from_id}: {e}")
        await message.answer(
            "❌ Произошла ошибка при создании заказа. Попробуйте позже."
        )


@bot.on.message(text="/my")
async def vk_my_order(message: Message) -> None:
    """
    VK handler for /my command.
    """
    try:
        user_id, first_name = await _get_vk_user(message)
        async with get_db() as db:
            user = await fetch_user(db, user_id, first_name)
            active = await get_active_order(db, user["user_id"])
            if not active:
                name_mention = format_vk_user_mention(
                    user_id, user.get("first_name") or first_name
                )
                await message.answer(
                    NO_ACTIVE_ORDER.format(name=name_mention)
                )
                return
            dishes = active["dishes"]
            lines = "\n".join(
                [DISH_LINE.format(name=n, crosses=v) for (n, v) in dishes]
            )
            total = sum(v for (_, v) in dishes)
            name_mention = format_vk_user_mention(
                user_id, user.get("first_name") or first_name
            )
            text = (
                f"👩‍🍳 {name_mention}, твой текущий заказ:\n\n{lines}"
                + ORDER_TOTAL.format(total=total)
            )
            await message.answer(text)
    except Exception as e:
        logger.error(f"Error viewing VK order for user {message.from_id}: {e}")
        await message.answer(
            "❌ Произошла ошибка при просмотре заказа. Попробуйте позже."
        )


@bot.on.message(text="/done")
async def vk_done(message: Message) -> None:
    """
    VK handler for /done command.
    """
    try:
        user_id, first_name = await _get_vk_user(message)
        async with get_db() as db:
            user = await fetch_user(db, user_id, first_name)
            active = await get_active_order(db, user["user_id"])
            if not active:
                name_mention = format_vk_user_mention(
                    user_id, user.get("first_name") or first_name
                )
                await message.answer(
                    NO_ACTIVE_ORDER.format(name=name_mention)
                )
                return

            order_crosses = sum(v for (_, v) in active["dishes"])
            (
                n_total,
                level_changed,
                new_title,
                total_crosses,
            ) = await finish_order_and_level(
                db, user["user_id"], active.get("tag"), order_crosses
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

        await message.answer(txt)
    except Exception as e:
        logger.error(f"Error completing VK order for user {message.from_id}: {e}")
        await message.answer(
            "❌ Произошла ошибка при завершении заказа. Попробуйте позже."
        )


@bot.on.message()
async def vk_fallback(message: Message) -> None:
    """
    Fallback handler for any text message.

    Helps to verify that the VK bot receives events at all.
    """
    logger.info(f"Received message in VK: from_id={message.from_id}, text={message.text!r}")
    if message.text in ("/start", "start", "начать"):
        await vk_start_handler(message)
    elif message.text in ("/new", "new"):
        await vk_new_order(message)
    elif message.text in ("/my", "my"):
        await vk_my_order(message)
    elif message.text in ("/done", "done"):
        await vk_done(message)
    else:
        await message.answer("Я бот Вышивального кафе во ВК. Напиши /start, /new, /my или /done.")


if __name__ == "__main__":
    logger.info("Starting VK bot...")
    # For vkbottle 2.7.x the recommended way is to call
    # the synchronous run_polling() helper which manages
    # the event loop internally.
    bot.run_polling()

