"""
Rules for claims on a dish the bill lists several of ("5 x Masala Dosa").

A person's claim is a number of whole units, or 0 for "sharing whatever
nobody claimed". For the claims to make sense:
  - the units claimed never exceed the bill's quantity;
  - while anyone is sharing, one unit stays unclaimed for them.
Sending a pick is checked against these (storage.submit_selection). When the
host lowers a dish's quantity, claims people already hold can stop fitting;
`fit_claims` brings them back.
"""
from typing import List


def fit_claims(quantity: int, claims: List[int]) -> List[int]:
    """What each holder's claim becomes when `quantity` is all there is.

    `claims` is each holder's units (0 = sharing), earliest joiner first. People
    keep their claim in that order while it fits; whoever comes after the last
    free unit gives up what is gone, down to sharing (0) when nothing is left,
    and a new sharer means one more unit has to stay back for them. A set of
    claims that already fits comes back unchanged. A dish the bill lists once
    is always shared equally, so its claims are never counted in units.
    """
    if quantity <= 1:
        return list(claims)
    sharing = any(units == 0 for units in claims)
    while True:
        free = quantity - (1 if sharing else 0)
        fitted = []
        for units in claims:
            kept = min(units, free) if units else 0
            free -= kept
            fitted.append(kept)
        pushed_out = any(kept == 0 and units > 0 for kept, units in zip(fitted, claims))
        if pushed_out and not sharing:
            sharing = True
            continue
        return fitted
