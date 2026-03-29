"""
Shared utilities for the bot.

Message formatting and admin checks.
"""
from config import ADMIN_IDS


def format_user_mention(user_id: int, first_name: str) -> str:
    """
    Format user as Telegram mention (clickable link).

    Args:
        user_id: Telegram user ID
        first_name: User first name

    Returns:
        HTML string with user mention
    """
    return f"<a href='tg://user?id={user_id}'>{first_name}</a>"


def is_admin(user_id: str) -> bool:
    """
    Check if user is a bot admin.

    Args:
        user_id: Telegram user ID (string)

    Returns:
        True if user is admin
    """
    return str(user_id) in ADMIN_IDS
