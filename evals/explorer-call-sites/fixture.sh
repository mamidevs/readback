#!/bin/bash
# Fixture: apply_discount has three callers. One reaches it through an aliased import, a registry and a dispatcher.
# A plain search for "apply_discount(" finds only the two direct callers.
set -e
cat > pricing.py <<'EOF'
def apply_discount(total, rate):
    return round(total * (1 - rate), 2)
EOF
cat > checkout.py <<'EOF'
from pricing import apply_discount


def checkout(cart):
    total = sum(item["price"] for item in cart)
    return apply_discount(total, 0.1)
EOF
cat > invoice.py <<'EOF'
import pricing


def invoice_total(lines, customer):
    subtotal = sum(line["amount"] for line in lines)
    rate = 0.2 if customer.get("vip") else 0.0
    return pricing.apply_discount(subtotal, rate)
EOF
cat > handlers.py <<'EOF'
from pricing import apply_discount as _discount

HANDLERS = {
    "discount": _discount,
}
EOF
cat > dispatch.py <<'EOF'
from handlers import HANDLERS


def run(action, *args):
    return HANDLERS[action](*args)
EOF
cat > jobs.py <<'EOF'
from dispatch import run


def nightly(totals):
    return [run("discount", t, 0.05) for t in totals]
EOF
cat > NOTES.md <<'EOF'
apply_discount was renamed from discount_total in 2024.
EOF
