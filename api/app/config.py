# app/config.py
from __future__ import annotations
import os, re
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

DATABASE_URL  = os.getenv("DATABASE_URL", "")
DB_SCHEMA     = os.getenv("DB_SCHEMA", "public")
ALLOW_ORIGINS = [o.strip() for o in os.getenv("ALLOW_ORIGINS", "*").split(",")]

def _mask(u: str | None) -> str:
    """Mascara senha na URL só para LOG (não altera a URL real)."""
    if not u:
        return ""
    return re.sub(r':[^:@/]+@', ':***@', u)

def as_asyncpg_url(url: str) -> str:
    """Converte postgres:// → postgresql+asyncpg:// e remove sslmode."""
    if not url:
        return url
    u = url.replace("postgres://", "postgresql://")
    if "postgresql+asyncpg://" not in u:
        u = u.replace("postgresql://", "postgresql+asyncpg://")

    pr = urlparse(u)
    qs = dict(parse_qsl(pr.query, keep_blank_values=True))

    # remove sslmode e força ssl=true quando fizer sentido
    sm = (qs.pop("sslmode", "") or "").lower()
    if sm in ("require", "verify-ca", "verify-full"):
        qs["ssl"] = "true"

    host = (pr.hostname or "")
    if "ssl" not in qs and any(k in host for k in ("azure","neon","supabase","amazonaws","render.com","cockroach")):
        qs["ssl"] = "true"

    return urlunparse(pr._replace(query=urlencode(qs)))

ASYNC_DATABASE_URL = as_asyncpg_url(DATABASE_URL)

# 🔁 Mapeamento UI → nomes da VIEW
UI_TO_DB = {
    "ENEL DISTRIBUIÇÃO SP": "ENEL_SP",
    "ENEL DISTRIBUIÇÃO RIO": "ENEL_RJ",
    "ELETROPAULO": "ENEL_SP",
    "LIGHT": "LIGHT",
    "ENERGISA": "ENERGISA",
    "NEOENERGIA": "NEOENERGIA",
    "EDP SP": "EDP_SP",
    "CPFL PAULISTA": "CPFL_PAULISTA",
    "CPFL PIRATININGA": "CPFL_PIRATININGA",
    "CPFL SANTA CRUZ": "CPFL_SANTA_CRUZ",
}

print("[api] DB URL (mask):", _mask(ASYNC_DATABASE_URL))
print("[api] Schema:", DB_SCHEMA)
