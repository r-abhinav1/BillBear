from textwrap import dedent

RECEIPT_JSON_FORMAT = (
    '{"restaurant":"[restaurant name]","date":"[date]","time":"[time]",'
    '"items":[{"name":"[item name]","price":"₹[price]"}],'
    '"subtotal":"₹[amount]","serviceCharge":"₹[amount]","discount":"₹[amount]",'
    '"cgst":"₹[amount]","sgst":"₹[amount]","igst":"₹[amount]","total":"₹[amount]"}'
)

RECEIPT_RULES = [
    "Output ONLY valid JSON in the format shown above.",
    "Do not include markdown, code fences, or explanations.",
    'Use "N/A" if any field is missing.',
    "All monetary values must include the rupee sign and two decimal places (example: ₹199.00).",
    'List all bill line-items under "items", each with "name" and "price".',
    (
        "CRITICAL — line item price: When a receipt table has columns like "
        "Item | Qty | Unit Price | Amount, use the AMOUNT column (rightmost/last price) "
        "as the price for that item — NOT the unit price. "
        "Example: 'Board Games  4  80.00  320.00' → price is ₹320.00, not ₹80.00."
    ),
    (
        "Tax aggregation: If the same tax type appears at multiple rates "
        "(e.g. CGST@9 and CGST@2.5, or SGST@9 and SGST@2.5), ADD them together into "
        "a single cgst or sgst field. Never put SGST into igst."
    ),
    "IGST is a separate central tax used instead of CGST+SGST — only use it if IGST is explicitly printed.",
    (
        "Total verification: sum all item prices; the result should equal the stated subtotal. "
        "If item prices sum to a value that matches the subtotal, you have the correct amounts. "
        "Use the stated grand total as the total field."
    ),
    "Restaurant name: extract the establishment name from the top of the receipt; do not leave it as N/A if it is present.",
]


def build_receipt_instruction_block() -> str:
    numbered_rules = "\n".join(f"{index + 1}. {rule}" for index, rule in enumerate(RECEIPT_RULES))
    return dedent(
        f"""
        You are a receipt parsing assistant. Return a single line, minified JSON object only.
        Expected JSON format:
        {RECEIPT_JSON_FORMAT}

        Rules:
        {numbered_rules}
        """
    ).strip()


def build_image_receipt_prompt() -> str:
    return build_receipt_instruction_block()


def build_text_receipt_prompt(raw_text: str) -> str:
    return (
        f"{build_receipt_instruction_block()}\n\n"
        "Normalize this OCR text into the JSON format:\n"
        f"{raw_text.strip()}"
    )

