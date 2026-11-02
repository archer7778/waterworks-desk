"""Units. Every reading is converted on the way in to one canonical unit per quantity, so no
calculation ever mixes m³/h with l/s, or bar with metres of water. Sensors keep a display unit
for people; the conversion back happens only when showing a number.

Canonical choices:
  pressure  m     metres of water head (pump head and level arithmetic stay in metres)
  flow      m3/h  (EPANET's CMH; what most SCADA in metric countries shows)
  level     m     depth or distance; becomes a level above datum through the sensor's mount level
"""
import math

G_FACTOR = 1 / 0.0980665          # 1 bar of water = 10.1972 m head (fresh water, standard gravity)

# quantity -> (canonical unit, {unit alias: (factor, offset)}); canonical = raw * factor + offset
QUANTITIES = {
    "pressure": ("m", {"m": (1, 0), "mh2o": (1, 0), "m_h2o": (1, 0), "mwc": (1, 0), "mwg": (1, 0),
                       "bar": (G_FACTOR, 0), "mbar": (G_FACTOR / 1000, 0), "kpa": (G_FACTOR / 100, 0),
                       "pa": (G_FACTOR / 100_000, 0), "mpa": (G_FACTOR * 10, 0), "psi": (0.70307, 0),
                       "ft": (0.3048, 0), "fth2o": (0.3048, 0)}),
    "flow": ("m3/h", {"m3/h": (1, 0), "cmh": (1, 0), "m3/s": (3600, 0), "m3/d": (1 / 24, 0),
                      "l/s": (3.6, 0), "lps": (3.6, 0), "l/min": (0.06, 0), "l/h": (0.001, 0),
                      "ml/d": (1000 / 24, 0), "mld": (1000 / 24, 0),
                      "gpm": (0.2271247, 0), "mgd": (157.7255, 0), "cfs": (101.9406, 0)}),
    "level": ("m", {"m": (1, 0), "cm": (0.01, 0), "mm": (0.001, 0), "ft": (0.3048, 0), "in": (0.0254, 0)}),
    "volume": ("m3", {"m3": (1, 0), "l": (0.001, 0), "ml": (1000, 0), "gal": (0.003785412, 0),
                      "mg": (3785.412, 0)}),
    "rain": ("mm", {"mm": (1, 0), "in": (25.4, 0)}),                 # depth in the reporting interval
    "rain_rate": ("mm/h", {"mm/h": (1, 0), "in/h": (25.4, 0)}),
    "temperature": ("degC", {"degc": (1, 0), "c": (1, 0), "°c": (1, 0),
                             "degf": (5 / 9, -32 * 5 / 9), "f": (5 / 9, -32 * 5 / 9), "°f": (5 / 9, -32 * 5 / 9),
                             "k": (1, -273.15)}),
    "current": ("A", {"a": (1, 0), "ma": (0.001, 0)}),
    "voltage": ("V", {"v": (1, 0), "kv": (1000, 0), "mv": (0.001, 0)}),
    "power": ("kW", {"kw": (1, 0), "w": (0.001, 0), "mw": (1000, 0), "hp": (0.7457, 0)}),
    "energy": ("kWh", {"kwh": (1, 0), "wh": (0.001, 0), "mwh": (1000, 0)}),
    "frequency": ("Hz", {"hz": (1, 0)}),
    "speed": ("rpm", {"rpm": (1, 0)}),
    "percent": ("%", {"%": (1, 0), "pct": (1, 0), "fraction": (100, 0)}),
    "vibration": ("mm/s", {"mm/s": (1, 0), "in/s": (25.4, 0)}),
    "run_hours": ("h", {"h": (1, 0), "min": (1 / 60, 0), "s": (1 / 3600, 0)}),
    "chlorine": ("mg/L", {"mg/l": (1, 0), "ppm": (1, 0)}),
    "turbidity": ("NTU", {"ntu": (1, 0), "fnu": (1, 0)}),
    "ph": ("pH", {"ph": (1, 0)}),
    "conductivity": ("uS/cm", {"us/cm": (1, 0), "ms/cm": (1000, 0)}),
    "state": ("state", {"state": (1, 0), "": (1, 0)}),             # coded: run/stop, open/shut, overflow on/off
    "count": ("count", {"count": (1, 0), "": (1, 0)}),
}


def _norm(u):
    return str(u or "").strip().lower().replace(" ", "").replace("³", "3").replace("µ", "u")


def canonical(quantity):
    if quantity not in QUANTITIES:
        raise ValueError(f"unknown quantity {quantity!r}; known: {', '.join(sorted(QUANTITIES))}")
    return QUANTITIES[quantity][0]


def known(quantity, unit):
    return quantity in QUANTITIES and _norm(unit) in QUANTITIES[quantity][1]


def converter(quantity, unit):
    """A function turning a value in `unit` into the quantity's canonical unit."""
    canonical(quantity)
    aliases = QUANTITIES[quantity][1]
    key = _norm(unit) if unit not in (None, "") else _norm(QUANTITIES[quantity][0])
    if key not in aliases:
        raise ValueError(f"{unit!r} is not a unit of {quantity} (try one of: {', '.join(sorted(a for a in aliases if a))})")
    factor, offset = aliases[key]
    if factor == 1 and offset == 0:
        return lambda v: v
    return lambda v: v * factor + offset


def from_canonical(quantity, unit, value):
    factor, offset = QUANTITIES[quantity][1][_norm(unit)]
    return (value - offset) / factor


def to_canonical(quantity, unit, value):
    return converter(quantity, unit)(value)


def finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
