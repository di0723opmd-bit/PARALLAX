"""Solveur local commun des défis Moltbook, repris tel quel de DakarMind v2.9.5 (bloc « solveur local v3 »).

Zéro appel LLM, bibliothèque standard seule. Au moindre doute : (None, raison) et le modèle de l'agent prend le relais.
La valeur retournée doit encore passer par le parse_challenge_answer de l'agent (format et décimales).
Garde héritée : deux réponses locales refusées de suite coupent le solveur local 24 h. ENABLE_LOCAL_SOLVER=False le coupe.
"""
import difflib
import logging
import os
import re
import time
from decimal import Decimal

logger = logging.getLogger("solveur_local")
OFF_SECONDS = 86400


LOCAL_EXPR_RE = re.compile(r"(?<![\w.])(-?\d+(?:\.\d+)?)\s*([+\-*/x×÷])\s*(-?\d+(?:\.\d+)?)(?![\w.])")
LOCAL_WORDY_RE = re.compile(
    r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|teen|twenty|thirty|forty|fifty|sixty|"
    r"seventy|eighty|ninety|hundred|thousand|half|twice|double|triple|percent|plus|minus|times|divided|multipl\w*|"
    r"add\w*|subtract\w*|sum|total|product|difference|then|each|per|remain\w*|more|less|fewer|gains?|loses?|"
    r"puis|ensuite|chaque|fois|moins|reste)\b",
    re.I,
)


# ==================== v2.9.5 : solveur local v3 (lecture double des défis bruités) ====================
# Principe : on ne répond que si DEUX lectures indépendantes du texte trouvent exactement les mêmes nombres et si une
# seule famille d'opération est établie. Lecture A : fragments voisins recollés (« tWeN tY », « T wO ») sur un vocabulaire
# fermé. Lecture B : concatenation de tout ce qui n'est pas un mot sûr, où l'on cherche des mots-nombres ; un nombre vu
# par une lecture seulement = abstention. Mesuré sur 106 défis réels (vérité = réponse acceptée par Moltbook) et sur
# 27 000 défis bruités générés : zéro erreur de lecture. Au moindre doute : None, Gemini Lite résout.

def sv3_collapse(s):
    return re.sub(r"(.)\1+", r"\1", s)


# --------------------------------------------------------------------------- vocabulaire fermé (forme écrasée)
_sv3_UNITS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
_sv3_TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
          "seventeen": 17, "eighteen": 18, "nineteen": 19}
_sv3_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
SV3_NUM = {}
for _sv3_w, _sv3_v in _sv3_UNITS.items():
    SV3_NUM[sv3_collapse(_sv3_w)] = ("u", _sv3_v)
for _sv3_w, _sv3_v in _sv3_TEENS.items():
    SV3_NUM[sv3_collapse(_sv3_w)] = ("t", _sv3_v)
for _sv3_w, _sv3_v in _sv3_TENS.items():
    SV3_NUM[sv3_collapse(_sv3_w)] = ("x", _sv3_v)
SV3_NUM[sv3_collapse("hundred")] = ("h", 100)
SV3_NUM[sv3_collapse("thousand")] = ("k", 1000)
for _sv3_w in ("zero", "million", "billion"):
    SV3_NUM[sv3_collapse(_sv3_w)] = ("big", 0)          # reconnus pour être vus, jamais calculés : abstention
assert len(SV3_NUM) == len(_sv3_UNITS) + len(_sv3_TEENS) + len(_sv3_TENS) + 5, "collision de formes écrasées"

SV3_CUE_ADD = {"adds", "add", "added", "adding", "gains", "gain", "gained", "gaining", "accelerates", "accelerated",
           "increases", "increased", "increasing", "speeds", "plus", "rises", "rose", "pushes", "combined",
           "together", "altogether", "sum", "boosts", "boosted"}
SV3_CUE_SUB = {"minus", "loses", "lose", "lost", "losing", "loss", "slows", "slowed", "slowing", "reduces", "reduced",
           "reducing", "decreases", "decreased", "decreasing", "drops", "dropped", "dropping", "falls", "fell",
           "decelerates", "subtract", "subtracted"}
SV3_CUE_DIV = {"divided", "divide", "dividing"}
SV3_CUE_MUL = {"multiplied", "multiplies", "multiply", "multiplying", "times", "product", "amplified", "amplifies",
           "amplify", "scaled", "scales", "double", "doubles", "doubled"}
SV3_WEAK_ADD = {"total"}                         # « total » seul n'affirme rien : il faut « and », « plus » ou une virgule
SV3_WEAK_SUB = {"remaining", "remains", "remain", "remained"}
# Ordre ou sens indéterminable : abstention immédiate (liste fermée).
SV3_BLOCK = {"each", "every", "half", "halves", "halved", "triple", "triples",
         "tripled", "twice", "thrice", "percent", "average", "ratio", "than", "net",
         "quotient", "square", "squared", "cubed", "cube", "convert", "dozen", "pair", "couple", "difference",
         "negative", "from", "less", "fewer", "away", "into", "equals", "equal", "gives", "give", "makes",
         "becomes", "become", "rather", "instead", "except", "unless", "least", "most", "ratio", "per cent",
         "reversed", "reverse", "opposite", "difference", "half", "if", "unlike"}
SV3_FILL = {"a", "an", "the", "is", "are", "of", "by", "to", "in", "on", "at", "it", "its", "and", "or", "um", "umm", "uh",
        "hmm", "huh", "hey", "err", "errr", "what", "whats", "how", "much", "many", "total", "force", "forces", "new",
        "claw", "claws", "lobster", "lobsters", "other", "another", "exerts", "exert", "exerted", "has", "have",
        "with", "that", "this", "swims", "swim", "speed", "velocity", "up", "after", "before", "molting", "while",
        "during", "dominance", "fight", "antenna", "antennas", "touches", "touching", "but", "so", "for", "as", "now",
        "left", "right", "per", "second", "seconds", "me", "ters", "meters", "meter", "newtons", "newton", "applies",
        "lunge", "engine", "mechanism", "lever", "arm", "torque", "pressure", "pascals", "water", "cool", "cold", "slowly"}
SV3_FUZZY_SAFE = ("antenna", "newtons", "lobster", "velocity", "pressure", "dominance")   # variantes bruitées tolérées
SV3_UNIT_WORDS = ("newtons", "meters", "pascals", "joules", "watts", "kilograms", "seconds", "centimeters")

_sv3_VOCAB = set(SV3_NUM) | {sv3_collapse(w) for w in (SV3_CUE_ADD | SV3_CUE_SUB | SV3_CUE_MUL | SV3_CUE_DIV | SV3_WEAK_ADD | SV3_WEAK_SUB | SV3_BLOCK | SV3_FILL)}
_sv3_CUE_C = {
    **{sv3_collapse(w): "+" for w in SV3_CUE_ADD}, **{sv3_collapse(w): "-" for w in SV3_CUE_SUB}, **{sv3_collapse(w): "*" for w in SV3_CUE_MUL},
    **{sv3_collapse(w): "/" for w in SV3_CUE_DIV},
}
_sv3_WEAK_C = {**{sv3_collapse(w): "+" for w in SV3_WEAK_ADD}, **{sv3_collapse(w): "-" for w in SV3_WEAK_SUB}}
_sv3_BLOCK_C = {sv3_collapse(w) for w in SV3_BLOCK}
_sv3_FILL_C = {sv3_collapse(w) for w in SV3_FILL} | {sv3_collapse(w) for w in SV3_WEAK_ADD | SV3_WEAK_SUB}
_sv3_NUM_BY_LEN = sorted(SV3_NUM, key=len, reverse=True)


def _sv3_clean_letters(raw):
    """Lettres seules, en minuscules, chiffre 0 lu comme « o » dans un mot mélangé. None si le jeton n'a pas de lettre."""
    letters = re.sub(r"[^A-Za-z0-9]", "", raw)
    if not letters:
        return None
    if re.search(r"[A-Za-z]", letters):
        letters = letters.replace("0", "o")
        if re.search(r"\d", letters):
            return "?" + letters.lower()            # chiffre étranger dans un mot : suspect
    return letters.lower()


SV3_SYMBOL_KINDS = ("noise", "op+", "op-", "op*", "opdiv", "opmix")


def _sv3_symbol_kind(raw):
    """Jeton fait uniquement de symboles : opérateur candidat (+ - *) ou bruit transparent."""
    core = re.sub(r"[^+\-*/×÷=−]", "", raw)
    if len(core) >= 2:                          # plusieurs caractères d'opération collés (« -* », « */ ») : ambigu
        return "opmix"
    if core in ("*", "×"):
        return "op*"
    if core == "+":
        return "op+"
    if core in ("-", "−"):
        return "op-"
    if core and set(core) <= set("/÷="):
        return "opdiv"
    return "noise"


def _sv3_segment_numbers(token):
    """« thirtytwo » -> ['thirty', 'two'] : seulement si TOUT le jeton se découpe en mots-nombres, d'une seule façon."""
    if not (6 <= len(token) <= 30):
        return None
    ways = [[] for _ in range(len(token) + 1)]
    ways[0] = [[]]
    for i in range(len(token)):
        if not ways[i]:
            continue
        for word in _sv3_NUM_BY_LEN:
            if token.startswith(word, i):
                ways[i + len(word)] = ways[i + len(word)] + [w + [word] for w in ways[i]]
    full = ways[len(token)]
    return full[0] if len(full) == 1 and len(full[0]) >= 2 else None


def _sv3_lev(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _sv3_fuzzy_number(token):
    """Mot-nombre abîmé (« tweny » pour twenty) : corrigé seulement si UN SEUL mot-nombre est à distance 1, avec
    même première et même dernière lettre, et au moins 5 lettres. Sinon None."""
    if len(token) < 5:
        return None
    near = [w for w in SV3_NUM if w != token and w[0] == token[0] and w[-1] == token[-1] and _sv3_lev(token, w) <= 1]
    return near[0] if len(near) == 1 else None


def _sv3_looks_like_number(token):
    """Mot inconnu trop proche d'un mot-nombre pour être ignoré : on s'abstient plutôt que de deviner."""
    if len(token) < 4:
        return False
    return any(difflib.SequenceMatcher(None, token, w).ratio() >= 0.8 for w in SV3_NUM if len(w) >= 4)


def _sv3_is_fuzzy_safe(token):
    return any(abs(len(token) - len(ref)) <= 3 and difflib.SequenceMatcher(None, token, ref).ratio() >= 0.72
               for ref in SV3_FUZZY_SAFE)


def _sv3_unit_like(token):
    return any(difflib.SequenceMatcher(None, token, sv3_collapse(ref)).ratio() >= 0.72 for ref in SV3_UNIT_WORDS)


def sv3_read_tokens(text):
    """Lecture A : liste de jetons classés. kind ∈ num, cue, weak, block, fill, other, op+, op-, op*, opdiv, noise."""
    raws = str(text or "").split()
    base = []                                                   # (kind, valeur) avant fusion
    for raw in raws:
        digits = re.fullmatch(r"[^\w]*(\d+(?:\.\d+)?)[^\w]*", raw)
        if digits:
            base.append({"kind": "digit", "value": Decimal(digits.group(1)), "c": digits.group(1)})
            continue
        letters = _sv3_clean_letters(raw)
        if letters is None:
            base.append({"kind": _sv3_symbol_kind(raw), "c": ""})
            continue
        base.append({"kind": "frag", "c": sv3_collapse(letters)})
    out = []
    i = 0
    while i < len(base):
        item = base[i]
        if item["kind"] != "frag":
            out.append(item)
            i += 1
            continue
        taken = False
        for width in (4, 3, 2, 1):                              # fusion de fragments voisins (le bruit transparent est sauté)
            pieces, j = [], i
            while j < len(base) and len(pieces) < width:
                if base[j]["kind"] == "frag":
                    pieces.append(base[j]["c"])
                elif base[j]["kind"] not in SV3_SYMBOL_KINDS:
                    break
                j += 1
            if len(pieces) != width:
                continue
            joined = sv3_collapse("".join(pieces))
            if width >= 2 and len(joined) < 3:
                continue
            if width >= 2 and sv3_collapse("".join(pieces[:-1])) == joined:
                continue        # dernier fragment avalé sans rien ajouter (« exerts s eVe nty ») : il ouvre le mot suivant
            if joined in _sv3_VOCAB:
                out.append({"kind": "word", "c": joined, "parts": width})
                i = j
                taken = True
                break
        if taken:
            continue
        parts = _sv3_segment_numbers(item["c"])
        if parts:
            for part in parts:
                out.append({"kind": "word", "c": part, "parts": 1})
        else:
            out.append({"kind": "other", "c": item["c"]})
        i += 1
    for item in out:
        if item["kind"] == "word":
            c = item["c"]
            if c in SV3_NUM:
                item["kind"], item["num"] = "num", SV3_NUM[c]
            elif c in _sv3_BLOCK_C:
                item["kind"] = "block"
            elif c in _sv3_CUE_C:
                item["kind"], item["fam"] = "cue", _sv3_CUE_C[c]
            elif c in _sv3_WEAK_C and c not in _sv3_FILL_C:
                item["kind"], item["fam"] = "weak", _sv3_WEAK_C[c]
            else:
                item["kind"] = "fill"
                if c in _sv3_WEAK_C:
                    item["weak"] = _sv3_WEAK_C[c]
        elif item["kind"] == "other":
            c = item["c"]
            if c.startswith("?"):
                item["kind"] = "suspect"
            elif _sv3_fuzzy_number(c):
                item["kind"], item["c"] = "num", _sv3_fuzzy_number(c)
                item["num"] = SV3_NUM[item["c"]]
            elif _sv3_is_fuzzy_safe(c):
                item["kind"] = "fill"
            elif _sv3_looks_like_number(c):
                item["kind"] = "suspect"
    return out


_sv3_CHUNK = r"(?:uh)?(?:xu?|t|u)?"
_sv3_NUMBER_RE = re.compile(r"(?:(?:" + _sv3_CHUNK + r")k)?" + _sv3_CHUNK)


def _sv3_chain_value(chain):
    total = cur = 0
    for kind, value in chain:
        if kind in ("u", "t", "x"):
            cur += value
        elif kind == "h":
            cur *= 100
        elif kind == "k":
            total += cur * 1000
            cur = 0
    return Decimal(total + cur)


def _sv3_group_numbers(seq):
    """Regroupe des mots-nombres consécutifs selon la grammaire anglaise (« thirty five », « two hundred and... » sans
    « and »). Un enchaînement invalide est coupé en nombres séparés. None si un mot « big » (zéro, million...) apparaît."""
    if any(kind == "big" for kind, _ in seq):
        return None
    values, i = [], 0
    while i < len(seq):
        for end in range(len(seq), i, -1):
            kinds = "".join(k for k, _ in seq[i:end])
            if kinds and _sv3_NUMBER_RE.fullmatch(kinds):
                values.append(_sv3_chain_value(seq[i:end]))
                i = end
                break
        else:                                   # « hundred » ou « thousand » isolé : pas de nombre
            return None
    return values


def _sv3_numbers_A(tokens):
    """Groupes de nombres de la lecture A : chaque groupe = [(kind, valeur)...] de mots-nombres que seul du bruit sépare."""
    groups, current = [], []
    for t in tokens:
        if t["kind"] == "num":
            current.append(t["num"])
        elif t["kind"] == "digit":
            if current:
                groups.append(current)
                current = []
            groups.append([("digit", t["value"])])
        elif t["kind"] == "noise":
            continue
        else:
            if current:
                groups.append(current)
                current = []
    if current:
        groups.append(current)
    return groups


def _sv3_values_of_groups(groups):
    values = []
    for group in groups:
        if group and group[0][0] == "digit":
            values.append(group[0][1])
            continue
        grouped = _sv3_group_numbers(group)
        if grouped is None:
            return None
        values.extend(grouped)
    return values


def _sv3_numbers_B(tokens):
    """Lecture B : on concatène les lettres de tout ce qui n'est pas un mot sûr et on y cherche des mots-nombres."""
    pieces = []
    for t in tokens:
        if t["kind"] == "num":
            pieces.append(t["c"])
        elif t["kind"] in ("other", "suspect"):
            pieces.append(t["c"].lstrip("?"))
        elif t["kind"] == "noise":
            continue
        elif t["kind"] == "digit":
            pieces.append("#")
        else:
            pieces.append("#")                                  # mot sûr : coupe la séquence
    s = sv3_collapse("".join(pieces))
    found, i = [], 0
    while i < len(s):
        for word in _sv3_NUM_BY_LEN:
            if s.startswith(word, i):
                found.append(SV3_NUM[word])
                i += len(word)
                break
        else:
            if s[i] == "#":
                found.append(("sep", 0))
            i += 1
    # un « sep » coupe les groupes ; on ne garde que les mots-nombres, en séparant par les « sep »
    groups, current = [], []
    for kind, value in found:
        if kind == "sep":
            if current:
                groups.append(current)
                current = []
        else:
            current.append((kind, value))
    if current:
        groups.append(current)
    return groups


def sv3_explain(text):
    """Pour le débogage : retourne (valeur, raison) ; valeur = Decimal ou None."""
    t = str(text or "").strip()
    if not t:
        return None, "vide"
    if t.endswith("…") or t.endswith("..."):
        return None, "texte tronqué"
    tokens = sv3_read_tokens(t)
    if any(x["kind"] in ("block", "suspect") for x in tokens):
        return None, "mot bloquant ou mot trop proche d'un nombre : " + ",".join(
            x.get("c", x["kind"]) for x in tokens if x["kind"] in ("block", "suspect"))

    def sig(index, step):
        index += step
        while 0 <= index < len(tokens) and tokens[index]["kind"] == "noise":
            index += step
        return tokens[index] if 0 <= index < len(tokens) else None

    for index, x in enumerate(tokens):
        if x["kind"] == "opdiv":
            before, after = sig(index, -1), sig(index, +1)
            if (before is not None and after is not None and after["kind"] in ("num", "digit")
                    and (before["kind"] in ("num", "digit") or before.get("c") and _sv3_unit_like(before["c"]))):
                return None, "division ou égalité entre deux quantités : non gérée"
            x["kind"] = "noise"                                 # sinon « / » est du bruit (très fréquent)
    # Fragment orphelin collé à un nombre : probablement un nombre coupé. Exception : suite de petits morceaux
    # qui forment ensemble une unité (« ne ooToNs », « cE mMeNtS »).
    for index, x in enumerate(tokens):
        if x["kind"] == "other" and len(x["c"]) <= 3:
            run, j = x["c"], index + 1
            while j < len(tokens) and len(run) < 12 and tokens[j]["kind"] in ("other", "noise"):
                if tokens[j]["kind"] == "other":
                    run += tokens[j]["c"]
                j += 1
            if len(sv3_collapse(run)) >= 4 and sig(index, -1) is not None and sig(index, -1)["kind"] in ("num", "digit"):
                continue                                        # morceau d'unité qui suit un nombre
            for step in (-1, 1):
                nb = sig(index, step)
                if nb is not None and nb["kind"] in ("num", "digit"):
                    return None, f"fragment orphelin « {x['c']} » collé à un nombre"
    groups_a = _sv3_numbers_A(tokens)
    values_a = _sv3_values_of_groups(groups_a)
    if values_a is None:
        return None, "grand nombre ou zéro"
    # Lecture B (relecture indépendante) : mêmes groupes exactement, sinon abstention.
    groups_b = _sv3_numbers_B(tokens)
    values_b = _sv3_values_of_groups(groups_b)
    if values_b is None or values_b != values_a:
        return None, f"lectures A/B différentes : {values_a} / {values_b}"
    if len(values_a) != 2:
        return None, f"{len(values_a)} nombres lus (il en faut exactement 2) : {values_a}"
    # Deux nombres exactement : on les repère dans la séquence de jetons pour contrôler l'ordre et l'opérateur.
    positions, in_group = [], False
    for index, x in enumerate(tokens):
        if x["kind"] in ("num", "digit"):
            if not in_group:
                positions.append(index)
            in_group = True
        elif x["kind"] != "noise":
            in_group = False
    # Séparation obligatoire entre deux nombres (sinon « thirty three seven » est ambigu).
    first_end = positions[0]
    while first_end + 1 < len(tokens) and tokens[first_end + 1]["kind"] in ("num", "digit", "noise"):
        first_end += 1
    between = [x for x in tokens[first_end + 1:positions[1]] if x["kind"] != "noise"]
    if not between:
        return None, "deux nombres collés sans séparation"
    # Unité : pour une addition ou une soustraction, les deux quantités doivent avoir la même unité (« meters » vs
    # « cm » : abstention). Pour un produit, des unités différentes sont normales (« 23 m/s * 4 newtons »).
    UNITISH = {sv3_collapse(w) for w in ("newtons", "newton", "meters", "meter", "metres", "metre", "pascals", "pascal",
                                     "joules", "watts", "kilograms", "grams", "volts", "amps")}

    def unit_after(end_index):
        k = end_index + 1
        while k < len(tokens) and tokens[k]["kind"] in SV3_SYMBOL_KINDS:
            k += 1
        first = tokens[k] if k < len(tokens) else None
        if first is None:
            return None
        end_index = k - 1
        if first["kind"] == "fill" and first.get("c") in UNITISH:
            return first["c"]
        if first["kind"] != "other":
            return None
        pieces, j = [], end_index + 1
        while j < len(tokens) and len(pieces) < 3:
            if tokens[j]["kind"] == "noise":
                j += 1
                continue
            if tokens[j]["kind"] != "other" or (pieces and len(tokens[j]["c"]) > 8):
                break
            pieces.append(tokens[j]["c"])
            j += 1
        return sv3_collapse("".join(pieces)) if pieces else None

    second_end = positions[1]
    while second_end + 1 < len(tokens) and tokens[second_end + 1]["kind"] in ("num", "digit", "noise"):
        second_end += 1
    unit_1, unit_2 = unit_after(first_end), unit_after(second_end)

    significant = sig

    # Plusieurs symboles d'opération entre les deux nombres (« -* - ») : lecture ambiguë.
    ops_between = [x for x in tokens[first_end + 1:positions[1]] if x["kind"] in ("op+", "op-", "op*", "opmix")]
    if len(ops_between) >= 2 or any(x["kind"] == "opmix" for x in ops_between):
        return None, "plusieurs symboles d'opération entre les deux nombres"
    # Opérateur.
    strong, weak, symbols = set(), set(), set()
    cue_between = set()
    for index, x in enumerate(tokens):
        if x["kind"] == "cue":
            strong.add(x["fam"])
            if first_end < index < positions[1]:
                cue_between.add(x["fam"])
        elif x["kind"] == "weak":
            weak.add(x["fam"])
        elif x.get("weak"):
            weak.add(x["weak"])
        elif x["kind"] in ("op+", "op-", "op*") and first_end < index < positions[1]:
            before, after = significant(index, -1), significant(index, +1)
            after_is_number = after is not None and after["kind"] in ("num", "digit")
            before_ok = before is not None and (before["kind"] in ("num", "digit") or (
                before["kind"] in ("other", "fill") and before.get("c") and (before["c"] in UNITISH or _sv3_unit_like(before["c"]))))
            if after_is_number and before_ok:
                symbols.add(x["kind"][2])                       # symbole autonome entre deux quantités : vrai opérateur
    if symbols:
        strong |= symbols
        cue_between |= symbols
    if len(strong) > 1:
        return None, f"indices d'opération contradictoires : {sorted(strong)}"
    values = values_a
    if strong:
        op = next(iter(strong))
        if op == "-" and "-" not in cue_between:
            return None, "soustraction dont le verbe n'est pas entre les deux nombres (ordre incertain)"
    else:
        if weak == {"+"}:
            joined_by_and = any(x.get("c") == "and" or x["kind"] == "op+" for x in tokens[first_end + 1:positions[1]])
            if not joined_by_and:
                return None, "« total » sans « and » ni « + » entre les deux nombres"
            unit_run = True
            for x in tokens[first_end + 1:positions[1]]:
                if x["kind"] == "other" and not unit_run:
                    return None, f"mot inconnu « {x['c']} » entre les deux nombres, avec « total » pour seul indice"
                if x["kind"] not in ("other", "noise") and not (x["kind"] == "fill" and x.get("c") in UNITISH):
                    unit_run = False
            op = "+"
        else:
            return None, "aucun indice d'opération"
    a, b = values
    if any(x.get("c") in ("doubl", "doubles", "doubled", "double") or x.get("c") == sv3_collapse("doubles") for x in tokens
           if x["kind"] == "cue") and not (op == "*" and b == 2):
        return None, "« doubles » accepté seulement pour « par deux »"
    if op in ("+", "-") and unit_1 and unit_2 and (
            unit_1[0] != unit_2[0] or difflib.SequenceMatcher(None, unit_1, unit_2).ratio() < 0.5):
        return None, f"unités différentes : {unit_1} / {unit_2}"
    if op == "+":
        result = a + b
    elif op == "-":
        result = a - b
        if result < 0:
            return None, "résultat négatif"
    elif op == "*":
        result = a * b
    elif op == "/":
        if b == 0 or "/" not in cue_between:
            return None, "division par zéro ou ordre incertain"
        result = a / b
        if result * b != a:
            return None, "division non exacte"
    else:
        return None, "opération non gérée"
    return result, f"{a} {op} {b}"

def solve(text):
    """(Decimal, lecture) ou (None, raison). Ne lève jamais d'exception."""
    text = str(text or "")
    if not text.strip():
        return None, "vide"
    if re.search(r"[√∛²³]|\d\s*[%^!]|[%^]\s*\d", text):
        return None, "symbole mathématique non géré (racine, puissance, pourcentage)"
    try:
        matches = LOCAL_EXPR_RE.findall(text)
        if len(matches) == 1 and len(re.findall(r"\d+(?:\.\d+)?", text)) == 2 and not LOCAL_WORDY_RE.search(text):
            left, operator, right = matches[0]
            a, b = Decimal(left), Decimal(right)
            if operator == "+":
                return a + b, "expression"
            if operator == "-":
                return a - b, "expression"
            if operator in ("*", "x", "×"):
                return a * b, "expression"
            return (a / b, "expression") if b else (None, "division par zéro")
        return sv3_explain(text)
    except Exception as exc:                       # le solveur ne doit JAMAIS faire échouer un défi
        logger.warning("Solveur local : exception ignorée (%s)", type(exc).__name__)
        return None, "exception"


def enabled(state, ts=None):
    """`state` : dict persistant de l'agent."""
    if os.getenv("ENABLE_LOCAL_SOLVER", "True").strip().lower() not in ("1", "true", "yes", "on"):
        return False
    return (time.time() if ts is None else ts) >= float(state.get("local_solver", {}).get("off_until", 0))


def feedback(state, ok, ts=None):
    """À appeler avec le verdict de /verify d'une réponse LOCALE."""
    mine = state.setdefault("local_solver", {})
    if ok:
        mine["fails"] = 0
        return
    mine["fails"] = int(mine.get("fails", 0)) + 1
    if mine["fails"] >= 2:
        mine["off_until"] = (time.time() if ts is None else ts) + OFF_SECONDS
        mine["fails"] = 0
        logger.warning("Solveur local : 2 réponses refusées de suite, coupé 24 h (modèle seul)")
