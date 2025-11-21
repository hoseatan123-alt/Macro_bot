import logging
import os
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ==============================
#   Mentzer calorie & macros
# ==============================

def mentzer_calories(weight_kg: float, goal: str) -> int:
    """
    Mike Mentzer-style calories (using bodyweight in lbs):
      Cut      -> 10 x bodyweight (lbs)
      Maintain -> 12 x bodyweight (lbs)
      Bulk     -> 15 x bodyweight (lbs)
    """
    goal = goal.strip().lower()
    weight_lbs = weight_kg * 2.20462

    factors = {
        "cut": 10,
        "maintain": 12,
        "bulk": 15,
    }

    if goal not in factors:
        raise ValueError("goal must be 'cut', 'maintain', or 'bulk'")

    calories = weight_lbs * factors[goal]
    return round(calories)


def get_macro_split(style: str):
    """
    Map a macro style string to (carb_pct, protein_pct, fat_pct)
    Styles:
      hc / highcarb / mentzer  -> 60C / 25P / 15F
      hp / highprotein         -> 40C / 35P / 25F
      hf / highfat             -> 30C / 25P / 45F
    """
    style = style.lower()

    alias_map = {
        "highcarb": "hc",
        "mentzer": "hc",
        "highprotein": "hp",
        "protein": "hp",
        "highfat": "hf",
        "fat": "hf",
    }

    if style in alias_map:
        style = alias_map[style]

    macro_styles = {
        "hc": (0.60, 0.25, 0.15),  # high carb
        "hp": (0.40, 0.35, 0.25),  # high protein
        "hf": (0.30, 0.25, 0.45),  # high fat
    }

    if style not in macro_styles:
        raise ValueError("Unknown macro style")

    return macro_styles[style], style


def mentzer_macros(
    calories: int,
    carb_pct: float,
    protein_pct: float,
    fat_pct: float,
) -> dict:
    """
    Split calories into carbs / protein / fats given percentages.
    Returns grams of each macro + metadata in a dict.
    """
    total_pct = carb_pct + protein_pct + fat_pct
    if abs(total_pct - 1.0) > 1e-6:
        raise ValueError("Macro percentages must add up to 1.0")

    carb_kcal = calories * carb_pct
    protein_kcal = calories * protein_pct
    fat_kcal = calories * fat_pct

    carb_g = round(carb_kcal / 4)
    protein_g = round(protein_kcal / 4)
    fat_g = round(fat_kcal / 9)

    return {
        "calories": calories,
        "carbs_g": carb_g,
        "protein_g": protein_g,
        "fats_g": fat_g,
        "carb_pct": carb_pct,
        "protein_pct": protein_pct,
        "fat_pct": fat_pct,
    }


def mentzer_plan(weight_kg: float, goal: str, macro_style: str = "hc") -> dict:
    """
    From weight + goal + macro style -> calories + macros.
    macro_style: 'hc', 'hp', 'hf' (or their aliases)
    """
    calories = mentzer_calories(weight_kg, goal)
    (carb_pct, protein_pct, fat_pct), style_key = get_macro_split(macro_style)
    macros = mentzer_macros(calories, carb_pct, protein_pct, fat_pct)
    macros["macro_style"] = style_key
    return macros


# ==============================
#   Food database (per 100 g raw)
# ==============================

FOODS_PER_100G = {
    # Carbs & carb-ish foods
    "oats_raw":        {"protein": 13.0, "carbs": 67.0, "fat": 7.0},
    "white_rice_raw":  {"protein": 7.0,  "carbs": 80.0, "fat": 0.6},
    "potatoes_raw":    {"protein": 2.0,  "carbs": 17.0, "fat": 0.1},
    "pasta_raw":       {"protein": 13.0, "carbs": 75.0, "fat": 1.5},
    "banana_raw":      {"protein": 1.1,  "carbs": 23.0, "fat": 0.3},
    "apple_raw":       {"protein": 0.3,  "carbs": 14.0, "fat": 0.2},

    # Protein-dominant
    "egg_whites_raw":  {"protein": 11.0, "carbs": 1.0,  "fat": 0.0},
    "chicken_breast_raw": {"protein": 31.0, "carbs": 0.0, "fat": 3.6},
    "beef_lean_raw":   {"protein": 26.0, "carbs": 0.0, "fat": 10.0},
    "salmon_raw":      {"protein": 20.0, "carbs": 0.0, "fat": 13.0},
    "tofu_firm_raw":   {"protein": 15.7, "carbs": 3.5,  "fat": 8.0},

    # Fats / mixed
    "olive_oil":       {"protein": 0.0,  "carbs": 0.0,  "fat": 100.0},
    "peanut_butter":   {"protein": 25.0, "carbs": 20.0, "fat": 50.0},
    "almonds_raw":     {"protein": 21.0, "carbs": 22.0, "fat": 49.0},
}

FOOD_LABELS = {
    "oats_raw": "🥣 Oats (raw)",
    "white_rice_raw": "🍚 White rice (raw)",
    "potatoes_raw": "🥔 Potatoes (raw)",
    "pasta_raw": "🍝 Pasta (raw)",
    "banana_raw": "🍌 Banana (raw)",
    "apple_raw": "🍎 Apple (raw)",

    "egg_whites_raw": "🥚 Egg whites (raw)",
    "chicken_breast_raw": "🐔 Chicken breast (raw)",
    "beef_lean_raw": "🥩 Lean beef (raw)",
    "salmon_raw": "🐟 Salmon (raw)",
    "tofu_firm_raw": "🧊 Firm tofu (raw)",

    "olive_oil": "🫒 Olive oil",
    "peanut_butter": "🥜 Peanut butter",
    "almonds_raw": "🌰 Almonds (raw)",
}

# Up to 6 different meal templates
MEAL_TEMPLATES = [
    {"label": "Breakfast", "protein": "egg_whites_raw",   "carb": "oats_raw",        "fat": "peanut_butter"},
    {"label": "Snack 1",   "protein": "tofu_firm_raw",    "carb": "banana_raw",     "fat": "almonds_raw"},
    {"label": "Lunch",     "protein": "chicken_breast_raw","carb": "white_rice_raw","fat": "olive_oil"},
    {"label": "Snack 2",   "protein": "beef_lean_raw",    "carb": "apple_raw",      "fat": "peanut_butter"},
    {"label": "Dinner",    "protein": "salmon_raw",       "carb": "potatoes_raw",   "fat": "olive_oil"},
    {"label": "Supper",    "protein": "egg_whites_raw",   "carb": "pasta_raw",      "fat": "almonds_raw"},
]


def grams_needed_for_macro(target_macro_g: float, macro_per_100g: float) -> float:
    """
    How many grams of a food are needed to get target_macro_g of a macro
    given macro_per_100g (grams per 100 g of that food)?
    """
    if macro_per_100g <= 0:
        return 0.0
    return target_macro_g / (macro_per_100g / 100.0)


def build_meal_from_foods(
    target_protein: float,
    target_carbs: float,
    target_fats: float,
    protein_food_key: str,
    carb_food_key: str,
    fat_food_key: str,
):
    """
    Build one meal using:
      - protein_food_key
      - carb_food_key
      - fat_food_key

    Strategy:
      1) Hit protein with protein food
      2) Hit remaining carbs with carb food
      3) Hit remaining fats with fat food
    """
    p_food = FOODS_PER_100G[protein_food_key]
    c_food = FOODS_PER_100G[carb_food_key]
    f_food = FOODS_PER_100G[fat_food_key]

    # 1) Protein food
    protein_food_g = grams_needed_for_macro(target_protein, p_food["protein"])
    p_from_p = p_food["protein"] * (protein_food_g / 100)
    c_from_p = p_food["carbs"] * (protein_food_g / 100)
    f_from_p = p_food["fat"] * (protein_food_g / 100)

    rem_carbs = max(target_carbs - c_from_p, 0)
    rem_fats = max(target_fats - f_from_p, 0)

    # 2) Carb food
    carb_food_g = grams_needed_for_macro(rem_carbs, c_food["carbs"])
    f_from_c = c_food["fat"] * (carb_food_g / 100)
    rem_fats = max(rem_fats - f_from_c, 0)

    # 3) Fat food
    fat_food_g = grams_needed_for_macro(rem_fats, f_food["fat"])

    items = [
        (protein_food_key, round(protein_food_g)),
        (carb_food_key, round(carb_food_g)),
        (fat_food_key, round(fat_food_g)),
    ]
    return items


def build_daily_meals(plan: dict, num_meals: int):
    """
    Split daily macros into multiple meals and give different food combos
    per meal, up to 6 meals.
    """
    num_meals = max(1, min(6, num_meals))

    per_meal_protein = plan["protein_g"] / num_meals
    per_meal_carbs = plan["carbs_g"] / num_meals
    per_meal_fats = plan["fats_g"] / num_meals

    meals = []

    for i in range(num_meals):
        template = MEAL_TEMPLATES[i % len(MEAL_TEMPLATES)]
        items = build_meal_from_foods(
            per_meal_protein,
            per_meal_carbs,
            per_meal_fats,
            template["protein"],
            template["carb"],
            template["fat"],
        )
        meals.append({"label": template["label"], "items": items})

    return meals


# ==============================
#   Telegram bot setup
# ==============================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def macro_style_human(style_key: str) -> str:
    if style_key == "hc":
        return "High carb (60C / 25P / 15F)"
    if style_key == "hp":
        return "High protein (40C / 35P / 25F)"
    if style_key == "hf":
        return "High fat (30C / 25P / 45F)"
    return style_key


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "💪 Mentzer Calorie & Macro Bot\n\n"
        "Send: <weight_kg> <goal> [macro_style] [meals]\n\n"
        "Goals:\n"
        "  cut / maintain / bulk\n\n"
        "Macro styles:\n"
        "  hc  – high carb (60C / 25P / 15F) [default]\n"
        "  hp  – high protein (40C / 35P / 25F)\n"
        "  hf  – high fat (30C / 25P / 45F)\n\n"
        "Meals:\n"
        "  1 to 6 (default 3)\n\n"
        "Examples:\n"
        "  75 cut\n"
        "  75 cut hp\n"
        "  80 bulk hf 5\n"
        "  70 maintain 4\n\n"
        "I'll give you calories, macros, and raw-gram suggestions for each meal."
    )
    await update.message.reply_text(text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Accepts messages like:
      '75 cut'
      '75 cut hp'
      '70 cut 4'
      '80 bulk hf 5'
    """
    if not update.message or not update.message.text:
        return

    msg = update.message.text.strip().lower()
    parts = msg.split()

    if len(parts) < 2 or len(parts) > 4:
        await update.message.reply_text(
            "Format:\n"
            "<weight_kg> <goal> [macro_style] [meals]\n"
            "Example: 75 cut hp 4"
        )
        return

    weight_str = parts[0]
    goal = parts[1]
    macro_style = "hc"
    meals = 3

    # Parse weight
    try:
        weight_kg = float(weight_str)
        if weight_kg <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Weight must be a positive number, e.g. `75 cut` or `75 cut hp 4`.",
            parse_mode="Markdown",
        )
        return

    # Interpret remaining tokens (style and/or meals)
    remaining = parts[2:]

    try:
        if len(remaining) == 1:
            token = remaining[0]
            if token.isdigit():
                meals = int(token)
            else:
                macro_style = token
        elif len(remaining) == 2:
            # Assume order: style then meals
            macro_style = remaining[0]
            if remaining[1].isdigit():
                meals = int(remaining[1])
            else:
                raise ValueError("Meals must be an integer 1–6.")
    except ValueError:
        await update.message.reply_text(
            "Check your input for meals (must be an integer 1–6)."
        )
        return

    if meals < 1 or meals > 6:
        await update.message.reply_text(
            "Meals must be between 1 and 6."
        )
        return

    # Build plan
    try:
        plan = mentzer_plan(weight_kg, goal, macro_style)
    except ValueError:
        await update.message.reply_text(
            "Check your input:\n"
            "- Goal: cut / maintain / bulk\n"
            "- Macro style: hc / hp / hf (or highcarb / highprotein / highfat)"
        )
        return

    # Build meals
    meals_data = build_daily_meals(plan, num_meals=meals)
    style_label = macro_style_human(plan["macro_style"])

    # Build reply text
    lines = []
    lines.append(f"⚙ Mentzer-style plan for {weight_kg:.1f} kg ({goal}):")
    lines.append("")
    lines.append(f"🔥 Calories: *{plan['calories']}* kcal/day")
    lines.append(f"🥧 Macro style: *{style_label}*")
    lines.append(f"🍽 Meals per day: *{meals}*")
    lines.append("")
    lines.append("📊 Daily macros:")
    lines.append(f"  • Carbs: *{plan['carbs_g']} g* (~{int(plan['carb_pct']*100)}%)")
    lines.append(f"  • Protein: *{plan['protein_g']} g* (~{int(plan['protein_pct']*100)}%)")
    lines.append(f"  • Fats: *{plan['fats_g']} g* (~{int(plan['fat_pct']*100)}%)")
    lines.append("")
    lines.append("🍱 Suggested meals (raw weights):")

    for meal in meals_data:
        lines.append(f"\n👉 {meal['label']}:")
        for food_key, grams in meal["items"]:
            label = FOOD_LABELS.get(food_key, food_key)
            lines.append(f"  • {label}: *{grams} g*")

    reply = "\n".join(lines)
    await update.message.reply_text(reply, parse_mode="Markdown")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)


def main():
    # 🔴 Put your bot token here
    BOT_TOKEN = os.environ.get("BOT_TOKEN")
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable not set")
    
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.add_error_handler(error_handler)

    app.run_polling()


if __name__ == "__main__":
    main()
