"""Internal beta CDF for dsRAG's fixed positive parameter pairs."""

from math import copysign, exp, lgamma, log, log1p


def beta_cdf(x: float, a: float, b: float) -> float:
    """Evaluate I_x(a, b); callers validate x and supply the fixed shape parameters.

    Uses the continued fraction and symmetry in https://dlmf.nist.gov/8.17.v
    (equations 8.17.22–23), evaluated with the modified Lentz recurrence.
    """
    if x == 0 or x == 1:
        return x
    reflected = x > (a + 1) / (a + b + 2)
    if reflected:
        x, a, b = 1 - x, b, a
    factor = exp(a * log(x) + b * log1p(-x)
                 + lgamma(a + b) - lgamma(a) - lgamma(b)) / a
    c = 1.0
    d = 1 / (1 - (a + b) * x / (a + 1))
    fraction = d
    for m in range(1, 201):
        even = m * (b - m) * x / ((a + 2 * m - 1) * (a + 2 * m))
        odd = -(a + m) * (a + b + m) * x / ((a + 2 * m) * (a + 2 * m + 1))
        for coefficient in (even, odd):
            d = 1 + coefficient * d
            c = 1 + coefficient / c
            # Avoid division by zero without changing the sign of a convergent.
            if abs(d) < 1e-300:
                d = copysign(1e-300, d)
            if abs(c) < 1e-300:
                c = copysign(1e-300, c)
            d = 1 / d
            change = c * d
            fraction *= change
        if abs(change - 1) <= 4e-15:
            value = factor * fraction
            return 1 - value if reflected else value
    raise ArithmeticError("Beta CDF did not converge")
