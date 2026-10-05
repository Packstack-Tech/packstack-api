"""Rules-first parser for user-typed variant text.

A CatalogVariant is a manufacturer option that can change the product's
weight: size, length, width, gender cut, capacity (litres, person count,
temperature rating, pack count, mAh...) or generation. Colour is aesthetic
and lives on the user's item (`Item.color`); anything else a user types into
the variant box ("w/ keys", "approx 750g", "900ml toaks") is a note.

`parse_variant(text)` splits the text into those three buckets with pure
rules. When a piece cannot be classified it is reported as `unknown`, and
the caller (catalog.enrich) asks a small model only then. The same function
drives the one-off cleanup of existing rows (workshop/catalog_variants) and
the live path (ensure_variant), so both produce the same vocabulary.

No database, no network, no model: fully unit-testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from models.keys import VARIANT_ALIASES, normalize_name

# Weight-bearing axes, in the order they appear in a combined name.
KINDS = ("gender", "size", "length", "width", "capacity", "generation")
KIND_ORDER = {k: i for i, k in enumerate(KINDS)}


@dataclass
class Axis:
    kind: str
    value: str          # canonical display spelling ("Women's", "Large", "2P", "20°F")


@dataclass
class ParsedVariant:
    source: str
    axes: list[Axis] = field(default_factory=list)
    color: str | None = None
    note: str | None = None
    unknown: list[str] = field(default_factory=list)   # pieces rules could not place
    decided_by: str = "rules"                            # or "ai" once a model filled it in
    weight_g: float | None = None                        # only ever set by the model

    @property
    def is_variant(self) -> bool:
        return bool(self.axes)

    @property
    def needs_ai(self) -> bool:
        return bool(self.unknown)

    @property
    def kind(self) -> str | None:
        if not self.axes:
            return None
        return self.axes[0].kind if len(self.axes) == 1 else "combo"

    @property
    def name(self) -> str | None:
        """Canonical variant name: axes joined in KIND order."""
        if not self.axes:
            return None
        ordered = sorted(self.axes, key=lambda a: KIND_ORDER[a.kind])
        return " / ".join(a.value for a in ordered)

    def as_dict(self) -> dict:
        return {
            "source": self.source, "name": self.name, "kind": self.kind,
            "axes": [(a.kind, a.value) for a in self.axes],
            "color": self.color, "note": self.note, "unknown": list(self.unknown),
            "decided_by": self.decided_by, "weight_g": self.weight_g,
        }


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

# canonical display for the keys VARIANT_ALIASES produces (plus bare ones)
_SIZE_CANON = {
    "xs": "XS", "small": "Small", "medium": "Medium", "large": "Large",
    "xl": "XL", "xxl": "XXL", "xxxl": "XXXL",
    "xss": "XS/S", "sm": "S/M", "ml": "M/L", "lxl": "L/XL", "xlxxl": "XL/XXL",
    "onesize": "One Size", "osfa": "One Size", "onesizefitsall": "One Size", "os": "One Size",
}
_LENGTH_CANON = {
    "regular": "Regular", "long": "Long", "short": "Short", "tall": "Tall", "petite": "Petite",
    "extralong": "Extra Long", "xlong": "Extra Long",
}
_WIDTH_CANON = {"wide": "Wide", "narrow": "Narrow", "regularwidth": "Regular Width", "wideregular": "Regular / Wide"}
_GENDER_CANON = {"men's": "Men's", "women's": "Women's", "unisex": "Unisex", "kids": "Kids", "junior": "Kids", "youth": "Kids"}

_COMBINED_CANON = {   # whole-token keys that mean two axes at once
    "regular / wide": [("length", "Regular"), ("width", "Wide")],
    "long / wide": [("length", "Long"), ("width", "Wide")],
}

# Colour words in the languages seen in the data (en, de, fr, nl, es, it).
_COLOR_WORDS = {
    "black", "white", "grey", "gray", "red", "blue", "green", "yellow", "orange", "purple", "pink",
    "brown", "tan", "beige", "navy", "olive", "khaki", "teal", "turquoise", "aqua", "cyan", "magenta",
    "charcoal", "graphite", "slate", "silver", "gold", "bronze", "copper", "ivory", "cream", "sand",
    "coyote", "sage", "forest", "lime", "mint", "maroon", "burgundy", "wine", "crimson", "scarlet",
    "coral", "salmon", "peach", "rust", "mustard", "ochre", "indigo", "violet", "lavender", "lilac",
    "plum", "mauve", "camo", "camouflage", "multicam", "woodland", "multi", "multicolor", "multicolour",
    "clear", "transparent", "smoke", "stealth", "shadow", "midnight", "stone", "pebble", "ash",
    "tangerine", "amber", "lemon", "moss", "pine", "spruce", "fern", "jade", "emerald",
    "cobalt", "royal", "sky", "ocean", "sea", "marine", "petrol", "denim", "chalk", "bone", "oatmeal",
    "heather", "melange", "anthracite", "obsidian", "onyx", "ebony", "jet", "pewter",
    "blaze", "fire", "flame", "sunset", "sunrise", "dusk", "dawn", "citrus", "glacier", "frost", "ice",
    "arctic", "alpine", "lichen", "fog", "mist", "storm", "thunder", "dune", "desert", "canyon", "clay",
    "terracotta", "brick", "cherry", "berry", "raspberry", "grape", "eggplant", "aubergine", "chocolate",
    "coffee", "espresso", "mocha", "caramel", "honey", "wheat", "straw", "harvest", "autumn", "fall",
    "dark", "light", "deep", "bright", "pale", "matte", "gloss", "neon", "pastel", "electric", "vivid",
    # de
    "schwarz", "weiss", "weiß", "grau", "rot", "blau", "grün", "gruen", "gelb", "braun", "dunkelgrau",
    "dunkelblau", "hellgrau", "hellblau", "karbongrau", "rauchschwarz", "anthrazit", "oliv",
    # fr
    "noir", "blanc", "gris", "rouge", "bleu", "vert", "jaune", "marron", "violet", "rose", "beige",
    # nl
    "zwart", "wit", "grijs", "rood", "blauw", "groen", "geel", "bruin", "paars", "oranje",
    # es / it
    "negro", "blanco", "rojo", "azul", "verde", "amarillo", "marrón", "nero", "bianco", "rosso", "grigio",
}

_NOTE_PREFIXES = re.compile(
    r"^(with|without|for|avec|sans|met|zonder|approx\.?|about|ca\.?|circa|incl\.?|including|"
    r"my|old|new|used|custom|modified|diy|myog)\b", re.IGNORECASE)
_WEIGHT_ONLY = re.compile(r"^~?\s*\d+([.,]\d+)?\s*(g|gr|gram|grams|kg|lb|lbs)$", re.IGNORECASE)

# capacity / measurement tokens
_PERSON = re.compile(r"^(\d)\s*[-]?\s*(p|person|people|man|pers)\.?$", re.IGNORECASE)
_PERSON_WORD = {"one": "1", "two": "2", "three": "3", "four": "4", "solo": "1"}
_PACK = re.compile(r"^(\d+)\s*[-]?\s*(pack|pk|pcs|pieces|count|ct|pair|pairs)$", re.IGNORECASE)
_PACK_REV = re.compile(r"^(pack|set)\s*of\s*(\d+)$", re.IGNORECASE)
_TEMP = re.compile(r"^([+-]?\d+)\s*(°|deg|degrees)?\s*([fc])\b(?:\s*/\s*([+-]?\d+)\s*(°|deg|degrees)?\s*([fc]))?$", re.IGNORECASE)
_UNIT = re.compile(
    r"^(\d+(?:[.,]\d+)?)\s*-?\s*(l|liter|liters|litre|litres|ltr|ml|oz|fl ?oz|mah|wh|w|gb|tb|lm|lumen|lumens|"
    r"ft|feet|foot|m|meter|meters|metre|metres|cm|mm|in|inch|inches|\"|''|yd|yds|mi|miles|km|ah|v|hz|"
    r"d|den|denier|mp|mpx|gal|gallon|gallons|qt|quart|quarts)\.?$", re.IGNORECASE)
_UNIT_CANON = {
    "liter": "L", "liters": "L", "litre": "L", "litres": "L", "ltr": "L", "l": "L", "ml": "mL",
    "oz": "oz", "fl oz": "fl oz", "floz": "fl oz", "mah": "mAh", "wh": "Wh", "w": "W", "gb": "GB", "tb": "TB",
    "lm": "lm", "lumen": "lm", "lumens": "lm", "ft": "ft", "feet": "ft", "foot": "ft", "m": "m",
    "meter": "m", "meters": "m", "metre": "m", "metres": "m", "cm": "cm", "mm": "mm", "in": "in",
    "inch": "in", "inches": "in", '"': "in", "''": "in", "yd": "yd", "yds": "yd", "mi": "mi", "miles": "mi",
    "km": "km", "gal": "gal", "gallon": "gal", "gallons": "gal", "qt": "qt", "quart": "qt", "quarts": "qt",
    "ah": "Ah", "v": "V", "hz": "Hz", "d": "D", "den": "D", "denier": "D", "mp": "MP", "mpx": "MP",
}
_NO_SPACE_UNITS = {"L", "mL", "mAh", "GB", "TB", "W", "Wh", "V", "D", "in", "MP", "Ah", "Hz"}
_DIMENSION = re.compile(r"^\d+(?:[.,]\d+)?\s*(?:x|×|\*)\s*\d+(?:[.,]\d+)?(?:\s*(?:x|×|\*)\s*\d+(?:[.,]\d+)?)?\s*(cm|mm|in|ft|m|\"|'|)$", re.IGNORECASE)
# shoe sizes: "US 10.5", "EU 43", "43 EU", "EU 43-45", "10.5 W"
_SHOE = re.compile(r"^(?:(us|uk|eu|eur|jp)\s*)?(\d{1,2}(?:[.,]5)?(?:\s*-\s*\d{1,2}(?:[.,]5)?)?)(?:\s*(us|uk|eu|eur|m|w|men|women|wide))?$", re.IGNORECASE)
_NUMBER = re.compile(r"^\d{1,2}(?:[.,]5)?$")

# generation
_YEAR = re.compile(r"^(19|20)\d{2}$")
_GEN = re.compile(r"^(?:gen(?:eration)?\s*\.?\s*(\d+)|(\d+)(?:st|nd|rd|th)?\s*gen(?:eration)?\.?|v\.?\s*(\d+(?:\.\d+)?)|mk\.?\s*(\d+)|version\s*(\d+))$", re.IGNORECASE)

_SPLIT = re.compile(r"\s*(?:/|,|;|\|| - | – | — |\+|&)\s*")
_SIZE_PREFIX = re.compile(r"^(size|sz|taille|größe|groesse|grösse|maat|talla|gr\.)\s*[:.]?\s*", re.IGNORECASE)
_PAREN = "\x01"   # marks a token that came from parentheses
_CODE = re.compile(r"^(?:[A-Z]{2,4}|\d{3,5})$")   # "(BK)", "(0040)": colour codes
_IGNORE = {"lengthen", "standard", "std", "default", "edition", "version", "fit", "cut", "original"}

_EXTRA_SIZE_WORDS = {   # parser-only spellings that are not safe as global aliases
    "klein": "small", "gross": "large", "groß": "large", "mittel": "medium",
    "petit": "small", "grand": "large", "moyen": "medium",
    "lang": "long", "lange": "long", "kurz": "short",
    "1x": "xl", "2x": "xxl", "3x": "xxxl", "xxs": "xxs", "2xs": "xxs",
}


# ---------------------------------------------------------------------------
# Token classification
# ---------------------------------------------------------------------------

def _classify_token(token: str) -> tuple[str, object] | None:
    """-> (bucket, payload). bucket in 'axes' | 'color' | 'note' | 'unknown'.
    payload: list[(kind, value)] for axes, str otherwise."""
    t = token.strip().strip("()[]").strip()
    if not t:
        return None
    if _WEIGHT_ONLY.match(t) or _NOTE_PREFIXES.match(t):
        return ("note", token.strip())

    sized = bool(_SIZE_PREFIX.match(t))
    t = _SIZE_PREFIX.sub("", t) or t
    k = normalize_name(t)
    if k in _EXTRA_SIZE_WORDS:
        k = _EXTRA_SIZE_WORDS[k]
    alias = VARIANT_ALIASES.get(k)
    if alias in _COMBINED_CANON:
        return ("axes", list(_COMBINED_CANON[alias]))
    ck = normalize_name(alias) if alias else k

    if ck in _SIZE_CANON:
        return ("axes", [("size", _SIZE_CANON[ck])])
    if ck == "xxs":
        return ("axes", [("size", "XXS")])
    if ck in _LENGTH_CANON:
        return ("axes", [("length", _LENGTH_CANON[ck])])
    if ck in _WIDTH_CANON:
        return ("axes", [("width", _WIDTH_CANON[ck])])
    for g, disp in _GENDER_CANON.items():
        if ck == normalize_name(g):
            return ("axes", [("gender", disp)])
    if re.fullmatch(r"[1-4]p", ck):
        return ("axes", [("capacity", ck.upper())])

    m = _PERSON.match(t)
    if m:
        return ("axes", [("capacity", f"{m.group(1)}P")])
    m = re.match(r"^(one|two|three|four)\s*[-]?\s*(p|person|people|man)$", t, re.IGNORECASE)
    if m:
        return ("axes", [("capacity", f"{_PERSON_WORD[m.group(1).lower()]}P")])
    m = _PACK.match(t)
    if m:
        return ("axes", [("capacity", f"{m.group(1)}-Pack")])
    m = _PACK_REV.match(t)
    if m:
        return ("axes", [("capacity", f"{m.group(2)}-Pack")])
    m = _TEMP.match(t)
    if m:
        a = f"{int(m.group(1))}°{m.group(3).upper()}"
        if m.group(4):
            a += f"/{int(m.group(4))}°{m.group(6).upper()}"
        return ("axes", [("capacity", a)])
    m = _UNIT.match(t)
    if m:
        num = m.group(1).replace(",", ".")
        unit = _UNIT_CANON.get(m.group(2).lower().replace(" ", ""), m.group(2))
        sep = "" if unit in _NO_SPACE_UNITS else " "
        return ("axes", [("capacity", f"{num}{sep}{unit}")])
    if _DIMENSION.match(t):
        return ("axes", [("capacity", re.sub(r"\s*(x|×|\*)\s*", " x ", t))])
    if _YEAR.match(t):
        return ("axes", [("generation", t)])
    m = _GEN.match(t)
    if m:
        n = next(g for g in m.groups() if g)
        if t.lower().startswith(("v", "version")):
            return ("axes", [("generation", f"v{n}")])
        if t.lower().startswith("mk"):
            return ("axes", [("generation", f"Mk{n}")])
        return ("axes", [("generation", f"Gen {n}")])

    # pure colour phrase: every word is a colour word ("Dark Petrol Blue").
    # Mixed phrases are handled word-by-word in _classify_words.
    words = re.findall(r"[^\W\d_]+", t.casefold())
    if words and all(w in _COLOR_WORDS for w in words) and not re.search(r"\d", t):
        return ("color", token.strip())

    # shoe / waist numbers only when they look like sizes ("US 10", "43 EU",
    # "Size 11"); a bare number could be a model number -> unknown.
    m = _SHOE.match(t)
    if m and (m.group(1) or m.group(3) or sized):
        return ("axes", [("size", re.sub(r"\s+", " ", t.upper()))])
    return ("unknown", token.strip())


def _split(text: str) -> list[str]:
    text = re.sub(r"\bw/o\b", "without", text, flags=re.IGNORECASE)
    text = re.sub(r"\bw/\s*", "with ", text, flags=re.IGNORECASE)
    # protect range sizes and dual temps before splitting on "/"
    protected = re.sub(r"(?<![\w'])(xs|s|m|l|xl|xxl)\s*/\s*(xs|s|m|l|xl|xxl)\b",
                       lambda m: f"{m.group(1)}\0{m.group(2)}", text, flags=re.IGNORECASE)
    protected = re.sub(r"([+-]?\d+\s*°?\s*[fc])\s*/\s*([+-]?\d+\s*°?\s*[fc])\b",
                       lambda m: f"{m.group(1)}\0{m.group(2)}", protected, flags=re.IGNORECASE)
    # a parenthetical is its own (marked) token: "Long Wide (25 x 72 in)"
    protected = re.sub(r"\s*\(([^()]*)\)\s*", lambda m: f" , {_PAREN}{m.group(1)} , ", protected)
    parts = [p.replace("\0", "/") for p in _SPLIT.split(protected)]
    return [p.strip() for p in parts if p and p.strip()]


def _classify_words(token: str) -> list[tuple[str, object]] | None:
    """Word-level pass for a token the whole-token rules rejected:
    'Men Red' -> gender + colour, 'Khaki Women's S' -> colour + gender + size,
    'Tarn Blue' / 'Black Walnut' -> one colour phrase. Returns None when a
    word cannot be placed (the token then goes to the model)."""
    words = token.split()
    if len(words) < 2 or len(words) > 5:
        return None
    classified = []
    for w in words:
        wl = re.sub(r"[^\w']", "", w.casefold())
        if wl in _IGNORE:
            continue
        if wl in _COLOR_WORDS:
            classified.append(("colorword", w.strip("(),")))
            continue
        c = _classify_token(w)
        classified.append(c if c and c[0] != "note" else ("unknown", w))
    if not classified:
        return None
    has_color = any(b == "colorword" for b, _ in classified)
    if not has_color:
        return None if any(b == "unknown" for b, _ in classified) else classified
    # colour present: unknown words adjacent to colour words join the phrase
    out: list[tuple[str, object]] = []
    run: list[str] = []
    for i, (b, p) in enumerate(classified):
        neighbour_color = (i > 0 and classified[i - 1][0] == "colorword") or \
                          (i + 1 < len(classified) and classified[i + 1][0] == "colorword")
        if b == "colorword" or (b == "unknown" and neighbour_color):
            run.append(p if isinstance(p, str) else str(p))
            continue
        if b == "unknown":
            return None
        if run:
            out.append(("color", " ".join(run))); run = []
        out.append((b, p))
    if run:
        if len(run) > 3:
            return None          # "Clear Scotchgard Anti-Fog Lens" is not a colour
        out.append(("color", " ".join(run)))
    return out


def parse_variant(text: str | None) -> ParsedVariant:
    pv = ParsedVariant(source=(text or "").strip())
    if not pv.source:
        return pv
    notes: list[str] = []
    colors: list[str] = []
    main_axes: list[Axis] = []
    paren_axes: list[Axis] = []

    for token in _split(pv.source):
        in_paren = token.startswith(_PAREN)
        token = token.lstrip(_PAREN).strip()
        if not token or token.casefold() in _IGNORE or (in_paren and _CODE.match(token)):
            continue
        c = _classify_token(token)
        if c is None:
            continue
        if c[0] == "unknown":
            pieces = _classify_words(token)
            if pieces:
                for p in pieces:
                    _absorb(pv, p, colors, notes, paren_axes if in_paren else main_axes)
                continue
        _absorb(pv, c, colors, notes, paren_axes if in_paren else main_axes)

    # a parenthetical measurement after a size is a spec, not a second axis:
    # "L (80 x 130 cm)" -> Large. Other parenthetical axes count normally.
    main_kinds = {a.kind for a in main_axes}
    for a in paren_axes:
        if a.kind == "capacity" and main_kinds & {"size", "length", "capacity"}:
            continue
        main_axes.append(a)

    # one value per axis; two sizes become a range ("Large/XL")
    by_kind: dict[str, list[str]] = {}
    for a in main_axes:
        vals = by_kind.setdefault(a.kind, [])
        if a.value not in vals:
            vals.append(a.value)
    for kind, vals in by_kind.items():
        if len(vals) == 1:
            pv.axes.append(Axis(kind, vals[0]))
        elif kind == "size" and len(vals) == 2:
            pv.axes.append(Axis(kind, "/".join(vals)))
        else:
            pv.unknown.append(f"{kind}: {' | '.join(vals)}")   # "3L, 5L, 8L": model decides
    pv.color = " / ".join(dict.fromkeys(colors)) or None
    pv.note = "; ".join(dict.fromkeys(notes)) or None
    return pv


def _absorb(pv: ParsedVariant, c, colors: list[str], notes: list[str], axes: list[Axis]) -> None:
    if c is None:
        return
    bucket, payload = c
    if bucket == "axes":
        axes.extend(Axis(k, v) for k, v in payload)
    elif bucket == "color":
        colors.append(payload)
    elif bucket == "note":
        notes.append(payload)
    else:
        pv.unknown.append(payload)
