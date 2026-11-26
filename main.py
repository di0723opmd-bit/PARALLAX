"""PARALLAX-4 v0.1 : méta-observateur pour Moltbook.
Registre de claims + détection de triangulation entre agents.
Hérite des garde-fous de VECTRA-3 (routeur multi-clés, disjoncteur de vérification,
anti-flatterie, liste blanche d'écriture) et de la discipline de ReliabilityRadar
(plafonds durs, fenêtres, warmup). Registre de claims propre (inspiré de la mémoire DakarMind).
"""
import json, logging, os, random, re, threading, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
import requests
from flask import Flask, jsonify

logger = logging.getLogger("parallax4")
VERSION = "0.1-observer"

def now(): return time.time()
def utc_dt(ts=None): return datetime.fromtimestamp(now() if ts is None else ts, timezone.utc)

# ============================== CONFIG ==============================
class Config:
    def __init__(self, env=None):
        e = os.environ if env is None else env
        g = lambda k, d="": str(e.get(k) or d).strip()
        b = lambda k, d: str(e.get(k, str(d))).lower() in ("1", "true", "yes", "on")
        self.moltbook_key = g("MOLTBOOK_API_KEY")
        self.agent_name = g("AGENT_NAME", "parallax4")
        self.moltbook_url = "https://www.moltbook.com/api/v1"
        self.state_dir = g("STATE_DIR", ".")
        self.dry_run = b("DRY_RUN", True)
        self.groq_key, self.gemini_key, self.openrouter_key = g("GROQ_API_KEY"), g("GEMINI_API_KEY"), g("OPENROUTER_API_KEY")
        self.groq_model = g("GROQ_MODEL", "openai/gpt-oss-120b")
        self.gemini_model = g("GEMINI_MODEL", "gemini-3.5-flash-lite")
        raw_or = [x.strip() for x in g("OPENROUTER_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free").split(",") if x.strip()]
        self.openrouter_models = [m for m in raw_or if m.lower().endswith(":free")]
        self.llm_calls_per_cycle = int(g("LLM_CALLS_PER_CYCLE", "3") or "3")
        self.llm_calls_per_day = int(g("LLM_CALLS_PER_DAY", "20") or "20")
        self.comments_per_hour, self.comments_per_day = 1, 6
        self.comment_gap_s = int(g("COMMENT_GAP_S", "1800") or "1800")
        self.posts_per_4h, self.posts_per_day = 1, 2
        self.post_gap_s = int(g("POST_GAP_S", "21600") or "21600")
        self.warmup_cycles = int(g("WARMUP_CYCLES", "6") or "6")
        self.startup_grace_s = int(g("STARTUP_GRACE_S", "600") or "600")
        self.windows = self._parse(g("ACTIVE_WINDOWS", "08:00-11:00,13:00-16:00,18:00-20:00"))
        self.post_windows = self._parse(g("POST_WINDOWS", "09:00-10:30,18:00-19:30"))
    def _parse(self, spec):
        out = []
        for p in spec.split(","):
            try:
                a, b = p.strip().split("-")
                h1, m1 = map(int, a.split(":")); h2, m2 = map(int, b.split(":"))
                s, e = h1*60+m1, h2*60+m2
                out.append((s, 1440 if e <= s else e))
            except ValueError: pass
        return out
    def in_window(self, ts=None):
        dt = utc_dt(ts); m = dt.hour*60 + dt.minute
        return any(a <= m < b for a, b in self.windows)
    def secrets(self):
        return [k for k in (self.moltbook_key, self.groq_key, self.gemini_key, self.openrouter_key) if k and len(k) >= 8]

def setup_logging(secrets=()):
    root = logging.getLogger(); root.setLevel(logging.INFO)
    if not root.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(h)
    logger.setLevel(logging.INFO)

# ============================== STATE ==============================
def default_state():
    return {"cycles": 0, "first_seen": now(), "identity_ok": False, "claim_status": "",
            "created_at": 0.0, "cold_start": True, "agents": {}, "threads": {},
            "claims": {}, "triangulations": [], "angles": [],
            "own_comments": [], "own_posts": [], "replied": [], "own_threads": {},
            "recent_outputs": [], "mentions": [], "last_summary": "",
            "llm": {"day": "", "calls": {}, "kcool": {}},
            "errors_streak": 0, "stats": {"created": 0, "verified": 0, "unverified": 0},
            "breaker": {"failures": 0, "paused_until": 0.0}}

class State:
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.lock = threading.RLock()
        self.d = default_state()
        self._load()
    def _load(self):
        if not self.path: return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self.d.update(loaded); self.d["cold_start"] = False
        except (FileNotFoundError, ValueError, OSError): pass
    def save(self):
        if not self.path: return True
        with self.lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                with tmp.open("w", encoding="utf-8") as f:
                    json.dump(self.d, f, ensure_ascii=False)
                tmp.replace(self.path); return True
            except (OSError, TypeError, ValueError): return False
    def add_event(self, kind, ts, **meta):
        with self.lock: self.d.setdefault(kind, []).append({"t": float(ts), **meta})
    def count(self, kind, window, ts=None):
        ts = now() if ts is None else ts
        return sum(1 for e in self.d.get(kind, []) if ts - e["t"] < window)
    def last(self, kind):
        ts = [e["t"] for e in self.d.get(kind, [])]
        return max(ts) if ts else None

# ============================== MOLTBOOK CLIENT ==============================
WRITE_PATHS = (re.compile(r"^/agents/[A-Za-z0-9_.\-]{1,64}/follow$"),
               re.compile(r"^/posts$"),
               re.compile(r"^/posts/[^/]{1,200}/comments$"),
               re.compile(r"^/verify$"))

class WriteForbidden(RuntimeError): pass

class Resp:
    def __init__(self, status, data=None):
        self.status, self.data = status, data
    @property
    def ok(self): return 200 <= self.status < 300

class MoltbookClient:
    def __init__(self, cfg, session=None):
        self.cfg = cfg; self.http = session or requests.Session(); self.write_pause = 0.0
    def _call(self, method, path, params=None, payload=None):
        if method != "GET":
            if method != "POST" or not any(rx.fullmatch(path) for rx in WRITE_PATHS):
                raise WriteForbidden(f"{method} {path}")
            if self.cfg.dry_run: raise WriteForbidden("DRY_RUN")
        if not self.cfg.moltbook_key: return None
        if now() < self.write_pause: return None
        try:
            r = self.http.request(method, f"{self.cfg.moltbook_url}{path}",
                                  headers={"Authorization": f"Bearer {self.cfg.moltbook_key}"},
                                  params=params, json=payload, timeout=20)
        except requests.RequestException as exc:
            logger.warning("%s %s : %s", method, path, type(exc).__name__); return None
        try: data = r.json()
        except ValueError: data = None
        if r.status_code == 429:
            try: wait = float(r.headers.get("Retry-After", 60))
            except (TypeError, ValueError): wait = 60.0
            if method != "GET": self.write_pause = now() + wait
        return Resp(r.status_code, data)
    def me(self): return self._call("GET", "/agents/me")
    def status(self): return self._call("GET", "/agents/status")
    def profile(self, n): return self._call("GET", "/agents/profile", {"name": n})
    def feed(self, filter="all", sort="new", limit=25):
        p = {"sort": sort, "limit": limit}
        if filter != "all": p["filter"] = filter
        return self._call("GET", "/feed", p)
    def posts(self, submolt, sort="hot", limit=15):
        return self._call("GET", "/posts", {"sort": sort, "limit": limit, "submolt": submolt})
    def comments(self, pid):
        r = self._call("GET", f"/posts/{quote(str(pid), safe='')}/comments", {"sort": "new", "limit": 100})
        if r is None or not r.ok or not isinstance(r.data, dict): return None
        return [c for c in _flatten(r.data.get("comments") or []) if not c.get("is_deleted")]
    def create_comment(self, pid, content, parent_id=None):
        p = {"content": content}
        if parent_id: p["parent_id"] = parent_id
        return self._call("POST", f"/posts/{quote(str(pid), safe='')}/comments", payload=p)
    def create_post(self, submolt, title, content):
        return self._call("POST", "/posts", payload={"submolt_name": submolt, "title": title[:300], "content": content})
    def verify(self, code, answer):
        return self._call("POST", "/verify", payload={"verification_code": code, "answer": answer})

def _flatten(items, parent="", depth=0):
    out = []
    if not isinstance(items, list) or depth > 8: return out
    for it in items:
        if not isinstance(it, dict): continue
        it = dict(it)
        if parent and not (it.get("parent_id") or it.get("parent_comment_id")):
            it["parent_id"] = parent
        out.append(it)
        for k in ("replies", "children"):
            if isinstance(it.get(k), list):
                out.extend(_flatten(it[k], str(it.get("id") or ""), depth+1))
    return out

def author_name(item):
    if not isinstance(item, dict): return ""
    a = item.get("author")
    if isinstance(a, dict): return str(a.get("name") or "")
    return str(a or item.get("author_name") or "")

def _entries(resp):
    if not resp or not resp.ok: return []
    d = resp.data
    if isinstance(d, list): return [x for x in d if isinstance(x, dict)]
    if isinstance(d, dict):
        for k in ("posts", "comments", "data", "results", "items"):
            if isinstance(d.get(k), list): return [x for x in d[k] if isinstance(x, dict)]
    return []

# ============================== LLM ROUTER ==============================
class LLMRouter:
    def __init__(self, cfg, state):
        self.cfg, self.state = cfg, state
        self.http = requests.Session(); self.disabled = set()
        self.cycle_calls = 0; self.degraded = False
    def begin_cycle(self): self.cycle_calls = 0
    @property
    def st(self):
        s = self.state.d["llm"]; today = utc_dt().date().isoformat()
        if s.get("day") != today:
            s["day"], s["calls"], s["kcool"] = today, {}, {}
        for k, v in (("calls", {}), ("kcool", {})):
            if not isinstance(s.get(k), type(v)): s[k] = v
        return s
    def _candidates(self):
        out = []
        if self.cfg.groq_key: out.append(("groq", "groq:key1", self.cfg.groq_model, self.cfg.groq_key))
        if self.cfg.gemini_key: out.append(("gemini", "gemini:key1", self.cfg.gemini_model, self.cfg.gemini_key))
        if self.cfg.openrouter_key:
            for m in self.cfg.openrouter_models:
                out.append(("openrouter", "openrouter:key1", m, self.cfg.openrouter_key))
        return out
    def blocked(self, provider, key_id, model):
        s = self.st
        if key_id in self.disabled: return "auth_disabled"
        if provider == "openrouter" and not model.lower().endswith(":free"): return "not_free"
        if s["kcool"].get(key_id, 0) > now(): return "cooldown"
        return None
    def any_available(self):
        return any(not self.blocked(p, k, m) for p, k, m, _ in self._candidates())
    def _call(self, provider, key_id, secret, model, system, user):
        if self.cycle_calls >= self.cfg.llm_calls_per_cycle: return None
        s = self.st
        if sum(s["calls"].values()) >= self.cfg.llm_calls_per_day: return None
        self.cycle_calls += 1
        s["calls"][key_id] = s["calls"].get(key_id, 0) + 1
        urls = {"groq": "https://api.groq.com/openai/v1",
                "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
                "openrouter": "https://openrouter.ai/api/v1"}
        try:
            r = self.http.post(f"{urls[provider]}/chat/completions", timeout=30,
                               headers={"Authorization": f"Bearer {secret}"},
                               json={"model": model,
                                     "messages": [{"role": "system", "content": system},
                                                  {"role": "user", "content": user}],
                                     "max_tokens": 800, "temperature": 0.5})
        except requests.RequestException as exc:
            logger.warning("LLM %s : %s", provider, type(exc).__name__); return None
        if r.status_code == 200:
            try:
                text = r.json()["choices"][0]["message"]["content"] or ""
                return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip() or None
            except (ValueError, KeyError, IndexError): return None
        if r.status_code in (401, 403): self.disabled.add(key_id); logger.error("Clé %s désactivée", key_id)
        elif r.status_code == 429: s["kcool"][key_id] = now() + 300
        elif r.status_code >= 500: s["kcool"][key_id] = now() + 120
        return None
    def ask(self, system, user):
        for provider, key_id, model, secret in self._candidates():
            if self.blocked(provider, key_id, model): continue
            out = self._call(provider, key_id, secret, model, system, user)
            if out: return out
        return None

# ============================== PARALLAX CORE : CLAIMS ==============================
class ClaimsRegistry:
    """Registre de claims : qui a affirmé quoi, quand, sous quelle source."""
    def __init__(self, state): self.state = state
    def _key(self, text):
        words = re.findall(r"[a-zà-ÿ0-9]{5,}", (text or "").lower())[:6]
        return " ".join(words[:4]) if len(words) >= 3 else ""
    def add(self, author, text, source, post_id=""):
        if not author or not text or len(text) < 20: return None
        key = self._key(text)
        if not key: return None
        d = self.state.d["claims"]; claim = d.get(key)
        if claim:
            claim["seen"] = claim.get("seen", 1) + 1
            if author not in claim["authors"]: claim["authors"].append(author)
            return claim
        claim = {"key": key, "text": text[:200], "authors": [author], "sources": [source],
                 "post_id": post_id, "first_seen": now(), "seen": 1}
        d[key] = claim
        if len(d) > 500:
            oldest = min(d.values(), key=lambda c: c["first_seen"])
            d.pop(oldest["key"], None)
        return claim
    def top(self, limit=20):
        return sorted(self.state.d["claims"].values(),
                      key=lambda c: (c.get("seen", 1), -c["first_seen"]), reverse=True)[:limit]

# ============================== PARALLAX CORE : TRIANGULATION ==============================
TENSION_RE = re.compile(r"\b(but|however|yet|actually|mais|pourtant|en réalité|sur le papier|except)\b", re.I)

class Triangulator:
    """Détecte deux claims qui se recoupent (même objet, angles différents) ou se contredisent."""
    def __init__(self, registry): self.registry = registry
    def find(self):
        claims = self.registry.top(30); best = None
        for i, a in enumerate(claims):
            for b in claims[i+1:]:
                if set(a["authors"]) == set(b["authors"]): continue
                wa = set(re.findall(r"[a-zà-ÿ]{4,}", a["text"].lower()))
                wb = set(re.findall(r"[a-zà-ÿ]{4,}", b["text"].lower()))
                shared = wa & wb
                if len(shared) < 2: continue
                relation = "contradiction" if bool(TENSION_RE.search(a["text"])) != bool(TENSION_RE.search(b["text"])) else "complémentarité"
                score = len(shared) + (a.get("seen", 1) + b.get("seen", 1)) * 0.5
                if not best or score > best["score"]:
                    best = {"claim_a": a, "claim_b": b, "relation": relation,
                            "shared": list(shared)[:5], "score": score}
        if best and len(best["claim_a"]["sources"]) + len(best["claim_b"]["sources"]) >= 2:
            return best
        return None

# ============================== OBSERVER ==============================
CORE = ["vina", "neo_konsi_s2bw", "lightningzero", "bytes", "diviner", "dynamo", "rossum"]
SIBLINGS = {"dakarmind", "reliabilityradar"}
EXCLUDED = {"codeofgrace"}

class Observer:
    def __init__(self, cfg, client, state, router):
        self.cfg, self.client, self.state, self.router = cfg, client, state, router
        self.me = cfg.agent_name
        self.registry = ClaimsRegistry(state)
        self.triangulator = Triangulator(self.registry)
        self.fatal = ""; self.fetched = []
    def bootstrap(self):
        if self.state.d["identity_ok"]: return True
        r = self.client.me()
        if r is None or not r.ok or not isinstance(r.data, dict): return False
        a = r.data.get("agent") if isinstance(r.data.get("agent"), dict) else r.data
        name = str(a.get("name") or "")
        if name.lower() != self.me.lower():
            self.fatal = f"Clé appartient à « {name} », pas {self.me} : arrêt"; return False
        self.state.d["identity_ok"] = True
        try: self.state.d["created_at"] = float(a.get("created_at") or 0)
        except (ValueError, TypeError): pass
        return True
    def refresh_claim(self):
        if self.state.d["claim_status"] == "claimed" and self.state.d["cycles"] % 10: return
        r = self.client.status()
        if r and r.ok and isinstance(r.data, dict):
            st = str(r.data.get("status") or "")
            if st and st != self.state.d["claim_status"]:
                logger.info("Statut du compte : %s", st)
                self.state.d["claim_status"] = st
    def collect(self):
        for name in CORE[:3]:
            r = self.client.profile(name)
            if not r or not r.ok or not isinstance(r.data, dict): continue
            a = r.data.get("agent") if isinstance(r.data.get("agent"), dict) else r.data
            for p in (r.data.get("recentPosts") or a.get("recentPosts") or []):
                if isinstance(p, dict): self._note_post(p, "profile")
            for c in (r.data.get("recentComments") or a.get("recentComments") or []):
                if isinstance(c, dict): self._note_comment(c, None)
        for p in _entries(self.client.feed(filter="following", sort="new", limit=20)):
            self._note_post(p, "feed:following")
        for sub in ("agents", "memory", "reliability"):
            for p in _entries(self.client.posts(sub, sort="hot", limit=10)):
                if isinstance(p, dict): p.setdefault("submolt_name", sub)
                self._note_post(p, f"sub:{sub}")
    def _note_post(self, p, source):
        pid = str(p.get("id") or p.get("post_id") or "")
        if not pid: return
        t = self.state.d["threads"].setdefault(pid, {"id": pid, "title": "", "author": "", "n": 0,
                                                     "fetched_at": 0, "last_seen": 0})
        t["title"] = str(p.get("title") or t["title"])[:160]
        t["author"] = author_name(p) or t["author"]
        try: t["n"] = int(p.get("comment_count") or p.get("comments_count") or t["n"])
        except (ValueError, TypeError): pass
        t["last_seen"] = now()
        self.registry.add(author_name(p), f"{p.get('title', '')} {p.get('content', '')}", source, pid)
    def _note_comment(self, c, p_id):
        author = author_name(c)
        if not author or author.lower() in SIBLINGS or author.lower() == self.me.lower(): return
        self.registry.add(author, str(c.get("content") or ""), "comment", p_id or "")
    def read_threads(self, limit=2):
        cands = [t for t in self.state.d["threads"].values() if now() - t["fetched_at"] > 1800]
        cands.sort(key=lambda t: (t["n"], t["last_seen"]), reverse=True)
        self.fetched = []
        for t in cands[:limit]:
            cmts = self.client.comments(t["id"]); t["fetched_at"] = now()
            if cmts is None: continue
            for c in cmts: self._note_comment(c, t["id"])
            self.fetched.append((t, cmts))
    def prepare_angle(self):
        d = self.state.d; tri = self.triangulator.find()
        if not tri: return None
        sig = f"{tri['claim_a']['key']}|{tri['claim_b']['key']}"
        if any(a.get("sig") == sig for a in d["angles"]): return None
        system = ("You are PARALLAX, a third-order observer on Moltbook. You see two claims made by different agents "
                  "that partially overlap. Describe what each agent sees that the other misses, and name the common "
                  "object. Do NOT judge who is right. Never flatter. No preamble.")
        user = (f"Claim A (by {tri['claim_a']['authors'][0]}): {tri['claim_a']['text']}\n\n"
                f"Claim B (by {tri['claim_b']['authors'][0]}): {tri['claim_b']['text']}\n\n"
                f"Shared terms: {', '.join(tri['shared'])}\nRelation: {tri['relation']}\n\n"
                "Reply with 3 lines only:\nTENSION: <one sentence>\nANGLE: <one sentence>\nCONFIDENCE: <1-5>")
        out = self.router.ask(system, user)
        if not out: return None
        m1 = re.search(r"TENSION\s*:\s*(.+)", out, re.I)
        m2 = re.search(r"ANGLE\s*:\s*(.+)", out, re.I)
        m3 = re.search(r"CONFIDENCE\s*:\s*(\d)", out, re.I)
        if not m1 or not m2: return None
        try: conf = max(1, min(5, int(m3.group(1)))) if m3 else 3
        except ValueError: conf = 3
        angle = {"sig": sig, "tension": m1.group(1).strip()[:300], "angle": m2.group(1).strip()[:500],
                 "confidence": conf, "t": now(), "tri": tri, "status": "brouillon"}
        d["angles"].append(angle); d["angles"] = d["angles"][-40:]
        logger.info("Angle préparé : conf=%s, relation=%s", conf, tri["relation"])
        return angle
    def cycle(self, actor=None):
        d = self.state.d; d["cycles"] += 1; self.router.begin_cycle()
        if not self.bootstrap():
            return self._done(self.fatal or "bootstrap incomplet")
        self.refresh_claim()
        self.collect(); self.read_threads()
        angle = self.prepare_angle()
        acted = actor.write_pass(self) if actor else "pas d'acteur"
        self.state.save()
        return self._done(f"claims={len(d['claims'])}, angles={len(d['angles'])}, "
                          f"angle {'préparé' if angle else 'aucun'}, écriture: {acted}")
    def _done(self, msg):
        self.state.d["last_summary"] = msg
        logger.info("Passage %s : %s", self.state.d["cycles"], msg)
        return msg

# ============================== ACTOR ==============================
BANNED = ("great point", "good point", "well put", "love this", "fascinating", "brilliant",
          "excellent", "magnifique", "incroyable", "bien dit", "bien vu", "tout à fait d'accord")
ENDS_CLEANLY = re.compile(r"[.!?…][\"'”’)\]»*_]*$")

class Actor:
    def __init__(self, cfg, client, state, router, observer):
        self.cfg, self.client, self.state, self.router, self.observer = cfg, client, state, router, observer
        self.me = cfg.agent_name
    def gate(self):
        d, cfg, ts = self.state.d, self.cfg, now()
        if not d["identity_ok"]: return "identité"
        if not cfg.dry_run and d["claim_status"] != "claimed": return "compte non réclamé"
        if d["cycles"] < cfg.warmup_cycles: return f"échauffement {d['cycles']}/{cfg.warmup_cycles}"
        if len(d["claims"]) < 3: return "registre de claims trop jeune"
        if ts - d["first_seen"] < cfg.startup_grace_s: return "délai de démarrage"
        if not cfg.in_window(ts): return "hors fenêtre"
        if self.state.d["breaker"]["paused_until"] > ts: return "disjoncteur de vérification"
        if not self.router.any_available(): return "aucun LLM disponible"
        return None
    def write_pass(self, observer):
        reason = self.gate()
        if reason:
            self.state.d["last_summary"] = f"écriture bloquée : {reason}"
            return reason
        angles = [a for a in self.state.d["angles"] if a["status"] == "brouillon"
                  and a["confidence"] >= 3 and now() - a["t"] < 12 * 3600]
        if not angles: return "aucun angle prêt"
        angle = max(angles, key=lambda a: (a["confidence"], a["t"]))
        text = self._generate(angle)
        if not text:
            angle["status"] = "abandonné"; return "génération refusée"
        pid = angle["tri"]["claim_a"].get("post_id")
        outcome = self._post_comment(pid, text) if pid and self._can_comment(pid) else self._post_new(text, angle)
        if outcome in ("ok", "unverified", "dry"):
            angle["status"] = "publié"
        return outcome
    def _can_comment(self, pid):
        last = self.state.last("own_comments")
        if last and now() - last < self.cfg.comment_gap_s: return False
        if self.state.count("own_comments", 3600) >= self.cfg.comments_per_hour: return False
        if self.state.count("own_comments", 86400) >= self.cfg.comments_per_day: return False
        if self.state.d["own_threads"].get(pid, 0) >= 1: return False
        return True
    def _generate(self, angle):
        tri = angle["tri"]
        system = ("You are PARALLAX. Write ONE short comment naming what two agents see differently. "
                  "2-3 finished sentences, under 350 chars. First sentence names the gap. Second names what "
                  "each sees or misses. Third (optional) names the common object or a testable question. "
                  "No flattery, no list, no greeting, no self-promotion. Same language as the thread. "
                  "Never say which agent is right. Never invent facts or numbers.")
        user = (f"Claim A (by {tri['claim_a']['authors'][0]}): {tri['claim_a']['text']}\n"
                f"Claim B (by {tri['claim_b']['authors'][0]}): {tri['claim_b']['text']}\n"
                f"Relation: {tri['relation']}\nTension: {angle['tension']}\nAngle: {angle['angle']}\n\n"
                "Reply with only the comment text.")
        out = self.router.ask(system, user)
        if not out: return None
        out = re.sub(r"^[\"'`«*\s]+|[\"'`»*\s]+$", "", out).strip()
        if not (60 <= len(out) < 380): return None
        if not ENDS_CLEANLY.search(out): return None
        low = out.lower()
        if any(b in low for b in BANNED): return None
        if re.search(r"^(@|hi|hello|salut|bonjour|hey)\b", low): return None
        if re.search(r"(you are parallax|system prompt|donnees_externes|as an ai)", low): return None
        if out in self.state.d["recent_outputs"]: return None
        return out
    def _post_comment(self, pid, text):
        ts = now()
        if self.cfg.dry_run:
            logger.info("DRY commentaire sur %s : %s", pid, text[:120])
            self.state.add_event("own_comments", ts, dry=True)
            self.state.d["own_threads"][pid] = self.state.d["own_threads"].get(pid, 0) + 1
            self.state.d["recent_outputs"].append(text)
            return "dry"
        r = self.client.create_comment(pid, text)
        if r is None or not r.ok: return "failed"
        self.state.add_event("own_comments", ts, post=pid)
        self.state.d["own_threads"][pid] = self.state.d["own_threads"].get(pid, 0) + 1
        self.state.d["recent_outputs"].append(text)
        self.state.d["stats"]["created"] += 1
        return self._settle(r.data, "comment")
    def _post_new(self, text, angle):
        ts = now(); title = angle["angle"][:100]
        if self.cfg.dry_run:
            logger.info("DRY post : %s / %s", title, text[:120])
            self.state.add_event("own_posts", ts, dry=True)
            self.state.d["recent_outputs"].append(text)
            return "dry"
        r = self.client.create_post("general", title, text)
        if r is None or not r.ok: return "failed"
        self.state.add_event("own_posts", ts)
        self.state.d["recent_outputs"].append(text)
        self.state.d["stats"]["created"] += 1
        return self._settle(r.data, "post")
    def _settle(self, data, label):
        v = self._extract_verification(data)
        if not v:
            self.state.d["stats"]["verified"] += 1; return "ok"
        answer = self._solve_challenge(v["challenge"], v.get("instructions", ""))
        if not answer:
            self.state.d["stats"]["unverified"] += 1; return "unverified"
        vr = self.client.verify(v["code"], answer)
        if vr and vr.ok:
            self.state.d["stats"]["verified"] += 1
            self.state.d["breaker"]["failures"] = 0
            logger.info("%s vérifié", label); return "ok"
        self.state.d["stats"]["unverified"] += 1
        self.state.d["breaker"]["failures"] += 1
        if self.state.d["breaker"]["failures"] >= 9:
            self.state.d["breaker"]["paused_until"] = now() + 6 * 3600
            logger.error("Disjoncteur : pause 6 h après 9 échecs")
        return "unverified"
    def _extract_verification(self, data):
        if not isinstance(data, dict): return None
        cands = [data.get("verification")] + [data[k].get("verification")
                                              for k in ("comment", "post", "submolt") if isinstance(data.get(k), dict)]
        for it in cands:
            if isinstance(it, dict):
                code = it.get("verification_code") or it.get("code")
                chal = it.get("challenge_text") or it.get("challenge")
                if code and chal:
                    return {"code": code, "challenge": chal, "instructions": it.get("instructions", "")}
        return None
    def _solve_challenge(self, text, instructions=""):
        system = ("Solve short obfuscated math word problems. Output ONLY a line 'RESULT: <number>' "
                  "on the last line. If instructions say '2 decimal places', use them.")
        user = f"Challenge:\n{text}\n\nInstructions:\n{instructions}"
        out = self.router.ask(system, user)
        if not out: return None
        m = re.findall(r"RESULT\s*:\s*([+-]?\d+(?:\.\d+)?)", out)
        return m[-1] if m else None

# ============================== APP ==============================
app = Flask(__name__)
_agent = None; _lock = threading.Lock()

class Agent:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state = State(os.path.join(cfg.state_dir, "parallax4_state.json"))
        self.client = MoltbookClient(cfg)
        self.router = LLMRouter(cfg, self.state)
        self.observer = Observer(cfg, self.client, self.state, self.router)
        self.actor = Actor(cfg, self.client, self.state, self.router, self.observer)
    def status(self):
        d = self.state.d
        return {"agent": self.cfg.agent_name, "version": VERSION,
                "mode": "dry_run" if self.cfg.dry_run else "live",
                "cycles": d["cycles"], "claim_status": d["claim_status"] or "?",
                "identity_ok": d["identity_ok"], "claims": len(d["claims"]),
                "angles": len(d["angles"]),
                "comments_24h": self.state.count("own_comments", 86400),
                "posts_24h": self.state.count("own_posts", 86400),
                "stats": d["stats"], "verify_failures": d["breaker"]["failures"],
                "last_summary": d["last_summary"], "fatal": self.observer.fatal}
    def run_forever(self):
        while not self.observer.fatal:
            try:
                self.observer.cycle(self.actor)
                self.state.d["errors_streak"] = 0
            except WriteForbidden as exc:
                logger.critical("Écriture refusée : %s", exc); self.observer.fatal = str(exc)
            except Exception as exc:
                self.state.d["errors_streak"] = self.state.d.get("errors_streak", 0) + 1
                logger.exception("Erreur : %s", type(exc).__name__)
            time.sleep(random.uniform(300, 420) * min(4, 1 + self.state.d.get("errors_streak", 0)))

def start_background():
    global _agent
    with _lock:
        if _agent is not None: return _agent
        cfg = Config(); setup_logging(cfg.secrets())
        if not cfg.moltbook_key:
            logger.critical("MOLTBOOK_API_KEY manquante"); return None
        missing = [p for p, k in (("GROQ", cfg.groq_key), ("GEMINI", cfg.gemini_key),
                                  ("OPENROUTER", cfg.openrouter_key)) if not k]
        if missing: logger.warning("Clés LLM manquantes : %s", ", ".join(missing))
        _agent = Agent(cfg)
        logger.info("PARALLAX-4 %s : %s", VERSION, "DRY_RUN (aucune écriture)" if cfg.dry_run else "LIVE")
        threading.Thread(target=_agent.run_forever, daemon=True).start()
        return _agent

@app.route("/")
@app.route("/healthz")
def health():
    return jsonify(_agent.status() if _agent else {"agent": "veille"})

if os.environ.get("PARALLAX_AUTOSTART", "1") == "1":
    start_background()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
