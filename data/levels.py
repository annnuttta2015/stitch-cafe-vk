# data/levels.py
LEVELS = {
    0: "🥄 Стажёр",
    1: "🍳 Помощник повара",
    2: "👨‍🍳 Повар",
    3: "🧑‍🍳 Су-шеф",
    4: "👩‍🍳 Шеф-повар",
}
ORDERS_PER_LEVEL = 10  # Level up every 10 orders
MAX_LEVEL = max(LEVELS.keys())
