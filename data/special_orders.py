"""
Special order events.

Game events (student, critic, dirty plate, second chef) with probabilities and messages.
"""
import random
from typing import cast

from data.texts import (
    CRITIC_APPEAR,
    DIRTY_PLATE_APPEAR,
    SECOND_CHEF_APPEAR,
    STUDENT_APPEAR,
)

SPECIAL_ORDERS = {
    "dirty_plate": {
        "text_template": DIRTY_PLATE_APPEAR,
        "probability": 0.15,
        "min_order_index": 3,
        "max_order_index": 40,
        "user_flag": "has_dirty_plate_done",
        "type": "double_previous",
    },
    "student": {
        "dish": ("🥡 Лапша быстрого приготовления", 100),
        "text_template": STUDENT_APPEAR,
        "probability": 0.12,
        "min_order_index": 3,
        "max_order_index": 40,
        "user_flag": "has_student_done",
        "type": "regular",
    },
    "critic": {
        "dish": ("🦪 Устрицы", 1000),
        "text_template": CRITIC_APPEAR,
        "probability": 0.10,
        "min_order_index": 20,
        "max_order_index": 40,
        "user_flag": "has_critic_done",
        "type": "regular",
    },
    "second_chef": {
        "text_template": SECOND_CHEF_APPEAR,
        "probability": 0.12,
        "min_order_index": 20,
        "max_order_index": 40,
        "user_flag": "has_second_chef_done",
        "type": "half_new_order",
    },
}

def check_special_order(
    order_index: int, user_flags: dict
) -> tuple[str, dict] | None:
    """
    Check whether a special order should be triggered.

    For each event: order index range, user flag (not done yet), then probability.

    Args:
        order_index: Current order number (1-based)
        user_flags: User flags (has_student_done, has_critic_done, etc.)

    Returns:
        (tag, order_config) if special order triggered, else None.
    """
    for tag, order_config in SPECIAL_ORDERS.items():
        min_idx = cast(int | None, order_config.get("min_order_index"))
        max_idx = order_config.get("max_order_index")
        if min_idx is not None and order_index < min_idx:
            continue
        if max_idx is not None and order_index > cast(int, max_idx):
            continue

        flag_name = cast(str, order_config["user_flag"])
        if user_flags.get(flag_name, 0):
            continue

        prob = cast(float, order_config["probability"])
        if random.random() < prob:
            return tag, order_config

    return None
