"""
Utilities specific to the VK version of the bot.

Currently contains helpers for formatting VK user mentions.
"""


def format_vk_user_mention(user_id: int, first_name: str) -> str:
    """
    Format user as VK mention for messages in communities/chats.

    Uses [id{user_id}|First name] syntax which is rendered as a clickable user link.
    """
    return f"[id{user_id}|{first_name}]"

