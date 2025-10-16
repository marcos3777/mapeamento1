# -*- coding: utf-8 -*-
"""
API que lê múltiplos *.gdb em DOWNLOADS_ROOT, faz join UCMT+PONNOT (pn_con == cod_id),
extrai lon/lat e expõe consultas por prefixo de lat/lon a partir do geocoding da CNPJá.

Fluxo:
- /health, /sources, /schema para inspeção
- /reload para reindexar quando você baixar novas pastas de concessionárias
- /join para paginação/inspeção do índice global (com filtros de origem)
- /v1/consulta/{cnpj}?decimals=3&limit=200[&estado=SP&concessionaria=Enel&ano=2024]
"""

import os
import re
import glob
import json
from datetime import datetime, timedelta
from threading import RLock
from typing import List, Optional, Dict, Any, Tuple

import fiona
import pandas as pd
import geopandas as gpd
import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────
DOWNLOADS_ROOT = os.getenv(
    "DOWNLOADS_ROOT",
    r"C:\Temp\admin\downloads"
)

LAYER_UCMT   = os.getenv("LAYER_UCMT", "")
LAYER_PONNOT = os.getenv("LAYER_PONNOT", "")

ENV_PATH = os.getenv(
    "ENV_PATH",
    r"C:\Temp\admin\api\.env"
)

TIMEOUT = 30
DEFAULT_DECIMALS = 3
DEFAULT_LIMIT = 200

CACHE_DIR = os.getenv("CACHE_DIR", "_cache")
CNPJA_CACHE_DIR = os.path.join(CACHE_DIR, "cnpja")
os.makedirs(CNPJA_CACHE_DIR, exist_ok=True)

JOINS_CACHE_DIR = os.path.join(CACHE_DIR, "joins")
os.makedirs(JOINS_CACHE_DIR, exist_ok=True)

# ──────────────────────────────────────────────────────────────────────────────
# App
# ──────────────────────────────────────────────────────────────────────────────
app = FastAPI(title="GDB Multi-Concessionárias API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
)

# Índice global em memória
DATA_JOIN_GLOBAL: Optional[pd.DataFrame] = None
META: Dict[str, Any] = {}

# ──────────────────────────────────────────────────────────────────────────────
# Utils GDB
# ──────────────────────────────────────────────────────────────────────────────
def discover_gdbs(root: str) -> List[str]:
    """Varre recursivamente o diretório e retorna lista de caminhos *.gdb"""
    pattern = os.path.join(root, "**", "*.gdb")
    return sorted(glob.glob(pattern, recursive=True))

def infer_source_meta(gdb_path: str) -> Dict[str, Optional[str]]:
    """
    Extrai metadados heurísticos do caminho (melhor esforço).
    Ex.: .../Enel_SP/Enel_SP_2024.gdb -> concessionaria=Enel, estado=SP, ano=2024
    """
    base = os.path.basename(gdb_path)           # Enel_SP_2024.gdb
    parent = os.path.basename(os.path.dirname(gdb_path))  # Enel_SP
    name = os.path.splitext(base)[0]            # Enel_SP_2024

    m_year = re.search(r"(20\d{2})", name)
    ano = m_year.group(1) if m_year else None

    tokens = re.split(r"[_\- ]+", name)
    estado = next((t for t in tokens if re.fullmatch(r"[A-Z]{2}", t)), None)

    conc = None
    for t in tokens:
        if t == estado:
            continue
        if ano and t == ano:
            continue
        conc = t
        break

    if not conc and parent:
        conc = parent

    return {
        "src_gdb": gdb_path,
        "src_concessionaria": conc,
        "src_estado": estado,
        "src_ano": ano,
    }

def find_layer(name_contains: str, layers: List[str]) -> str:
    key = name_contains.lower()
    for L in layers:
        if key in L.lower():
            return L
    raise ValueError(f"Não achei layer contendo '{name_contains}'. Disponíveis: {layers}")

def ensure_wgs84(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if gdf.crs is None:
        raise ValueError("Layer sem CRS; defina o CRS correto antes do reprojeto.")
    if gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)
    return gdf

def build_join_from_gdb_path(gdb_path: str) -> pd.DataFrame:
    layers = fiona.listlayers(gdb_path)
    ucmt_layer   = LAYER_UCMT or find_layer("UCMT", layers)
    ponnot_layer = LAYER_PONNOT or find_layer("PONNOT", layers)

    ucmt   = gpd.read_file(gdb_path, layer=ucmt_layer)
    ponnot = gpd.read_file(gdb_path, layer=ponnot_layer)

    ucmt.columns   = [c.strip() for c in ucmt.columns]
    ponnot.columns = [c.strip() for c in ponnot.columns]

    col_pn_con_ucmt   = next((c for c in ucmt.columns   if c.lower() == "pn_con"), None)
    col_cod_id_ponnot = next((c for c in ponnot.columns if c.lower() == "cod_id"), None)
    if not col_pn_con_ucmt or not col_cod_id_ponnot:
        raise ValueError(
            f"Chaves não encontradas no GDB {gdb_path}. "
            f"UCMT: {ucmt.columns.tolist()} | PONNOT: {ponnot.columns.tolist()}"
        )

    ponnot = ensure_wgs84(ponnot)
    if not ponnot.geom_type.isin(["Point"]).all():
        ponnot["geometry"] = ponnot.geometry.centroid

    ponnot["lon"] = ponnot.geometry.x
    ponnot["lat"] = ponnot.geometry.y
    pon_small = ponnot[[col_cod_id_ponnot, "lon", "lat"]].copy()

    joined = ucmt.merge(
        pon_small,
        left_on=col_pn_con_ucmt,
        right_on=col_cod_id_ponnot,
        how="left"
    ).drop(columns=[col_cod_id_ponnot], errors="ignore")

    dic_cols = [c for c in joined.columns if c.upper().startswith("DIC_")]
    fic_cols = [c for c in joined.columns if c.upper().startswith("FIC_")]
    for col in dic_cols + fic_cols:
        joined[col] = pd.to_numeric(joined[col], errors="coerce")
    if dic_cols:
        joined["dic_med"] = joined[dic_cols].mean(axis=1, skipna=True)
    if fic_cols:
        joined["fic_med"] = joined[fic_cols].mean(axis=1, skipna=True)

    if "geometry" in joined.columns:
        joined = pd.DataFrame(joined.drop(columns=["geometry"]))

    for col in ("lon", "lat"):
        if col in joined.columns:
            joined[col] = pd.to_numeric(joined[col], errors="coerce")

    meta = infer_source_meta(gdb_path)
    for k, v in meta.items():
        joined[k] = v

    return joined

def _parquet_path_for(gdb_path: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", os.path.relpath(gdb_path, DOWNLOADS_ROOT))
    return os.path.join(JOINS_CACHE_DIR, safe + ".parquet")

def build_global_index() -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    Descobre GDBs, cria/usa cache de cada join e concatena tudo.
    Retorna (DATA_JOIN_GLOBAL, META)
    """
    gdbs = discover_gdbs(DOWNLOADS_ROOT)
    frames: List[pd.DataFrame] = []
    sources: List[Dict[str, Any]] = []

    for gdb in gdbs:
        pqt = _parquet_path_for(gdb)
        use_cache = False
        try:
            if os.path.exists(pqt) and os.path.getmtime(pqt) >= os.path.getmtime(gdb):
                use_cache = True
        except Exception:
            pass

        try:
            if use_cache:
                df = pd.read_parquet(pqt)
            else:
                df = build_join_from_gdb_path(gdb)
                df.to_parquet(pqt, index=False)   # cache parquet (snappy por padrão com pyarrow)
        except Exception as e:
            print(f"[WARN] Falha ao processar {gdb}: {e}")
            continue

        frames.append(df)
        src = infer_source_meta(gdb)
        src["rows"] = len(df)
        sources.append(src)

    if not frames:
        raise RuntimeError(f"Nenhum GDB processado. Verifique {DOWNLOADS_ROOT}.")

    all_df = pd.concat(frames, ignore_index=True)
    meta = {
        "downloads_root": DOWNLOADS_ROOT,
        "gdb_count": len(gdbs),
        "rows": len(all_df),
        "columns": all_df.columns.tolist(),
        "sources": sources,   # lista com {src_gdb, src_concessionaria, src_estado, src_ano, rows}
    }
    return all_df, meta

# ──────────────────────────────────────────────────────────────────────────────
# Utils CNPJá
# ──────────────────────────────────────────────────────────────────────────────
def first_of(data: Dict[str, Any], paths: List[str], default: Any = "") -> Any:
    for path in paths:
        cur: Any = data
        ok = True
        for p in path.split("."):
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                ok = False
                break
        if ok and cur not in (None, "", [], {}):
            return cur
    return default
def build_cnpja_selection(data: Dict[str, Any]) -> Dict[str, Any]:
    taxId   = first_of(data, ["taxId", "company.taxId"])
    name    = first_of(data, ["name", "company.name", "legalName"])
    alias   = first_of(data, ["alias", "company.alias", "tradeName", "nomeFantasia"])
    founded = first_of(data, ["founded", "company.founded", "opening", "company.opening"])
    equity  = first_of(data, ["equity", "company.equity"])
    size    = first_of(data, ["size", "company.size"])  
    # Objetos aninhados
    nature_text = first_of(data, ["nature.text", "company.nature.text"])
    status_text = first_of(data, ["status.text", "company.status.text", "status"])
    reason_text = first_of(data, ["reason.text"])
    status_date = first_of(data, ["statusDate"])  
    special     = first_of(data, ["special.text"])     
    specialDate = first_of(data, ["specialDate"])
    # Address (string formatada) + country.name
    address_str = join_nonempty([
        first_of(data, ["address.street"]),
        first_of(data, ["address.number"]),
        first_of(data, ["address.district"]),
        first_of(data, ["address.city"]),
        first_of(data, ["address.state"]),
        first_of(data, ["address.zip"]),
    ])
    country_name = first_of(data, ["address.country.name", "country.name"])

    # Phones (lista)
    phones_raw = data.get("phones") or []
    phones = []
    for p in phones_raw if isinstance(phones_raw, list) else []:
        phones.append({
            "area":   p.get("area"),
            "number": p.get("number"),
            "type":   p.get("type"),
        })

    # Emails (lista – só os endereços)
    emails_raw = data.get("emails") or []
    emails = []
    for e in emails_raw if isinstance(emails_raw, list) else []:
        addr = e.get("address")
        if addr:
            emails.append(addr)

    # Atividades
    mainActivity = None
    ma = data.get("mainActivity") or {}
    if isinstance(ma, dict):
        mainActivity = {"id": ma.get("id"), "text": ma.get("text")}

    sideActivities = []
    sa = data.get("sideActivities") or []
    if isinstance(sa, list):
        for it in sa:
            if isinstance(it, dict):
                sideActivities.append({"id": it.get("id"), "text": it.get("text")})
    
    
    simples = None
    if isinstance(data.get("simples"), dict):
        s = data["simples"]
        simples = {
            "optant": s.get("optant"),
            "mei": s.get("mei"),
            "since": s.get("since"),
            "exclusionDate": s.get("exclusionDate"),
            "exclusionDate": s.get("exclusionReason")
        }

    simplesHistory = []
    if isinstance(data.get("simplesHistory"), list):
        for h in data["simplesHistory"]:
            if isinstance(h, dict):
                simplesHistory.append({
                    "optant": h.get("optant"),
                    "mei": h.get("mei"),
                    "since": h.get("since"),
                    "exclusionDate": h.get("exclusionDate"),
                    "exclusionReason": h.get("exclusionReason")
                })
    registrations = []
    if isinstance(data.get("registrations"), list):
        for r in data["registrations"]:
            if isinstance(r, dict):
                registrations.append({
                    "number":r.get("number"),
                    "state": r.get("state"),
                    "enabled": r.get("enabled"),
                    "statusDate": r.get("statusDate"),
                    "status":(r.get("status") or {}).get("text"),
                    "type":(r.get("type") or {}).get("text"),
                })

    suframa = []
    if isinstance(data.get("suframa"), list):
        for s in data["suframa"]:
            if isinstance(s, dict):
                incentives = []
                if isinstance(s.get("incentives"), list):
                    for inc in s["incentives"]:
                        if isinstance(inc, dict):
                            incentives.append({
                                "tribute": inc.get("tribute"),
                                "benefit": inc.get("benefit"),
                                "purpose": inc.get("purpose"),
                                "basis": inc.get("basis"),
                            })
                suframa.append({
                    "number":s.get("number"),
                    "since": s.get("since"),
                    "approved": s.get("approved"),
                    "approvalDate": s.get("approvalDate"),
                    "status":(s.get("status") or {}).get("text"),
                    "incentives": incentives
                })
    # Membros
    members = []
    mem = first_of(data,["company.members", "members"], default=[])
    if isinstance(mem, list):
        for m in mem:
            if not isinstance(m, dict):
                continue
            person = m.get("person") or {}
            role   = m.get("role")   or {}
            members.append({
                "since": m.get("since"),
                "person": {
                    "name": person.get("name"),
                    "type": person.get("type"),
                    "taxId": person.get("taxId"),
                    "age":   person.get("age"),
                },
                "role": {
                    "id":   role.get("id"),
                    "text": role.get("text"),
                },
                "agent":{
                    "person":(m.get("agent") or {}).get("person"),
                    "role": (m.get("agent") or {}).get("role"),
                }
            })

    return {
        "taxId": taxId,
        "name": name,
        "alias": alias,
        "founded": founded,
        "equity": equity,
        "size": size,
        "nature.text": nature_text,
        "status.text": status_text,
        "status.date": status_date,
        "status.reason": reason_text,
        "special": special,
        "specialDate": specialDate,
        "address": address_str,
        "country.name": country_name,
        "phones": phones,
        "emails": emails,
        "mainActivity": mainActivity,
        "sideActivities": sideActivities,
        "simples": simples,
        "simplesHistory": simplesHistory,
        "registrations": registrations,
        "suframa": suframa,
        "members": members,
    }

def join_nonempty(parts, sep=", "):
    return sep.join([str(p) for p in parts if p not in (None, "", [], {})])

def latlon_prefix(value: Any, decimals: int) -> Optional[str]:
    if value is None:
        return None
    try:
        return f"{float(value):.{decimals}f}"
    except Exception:
        return None

# cache em memória: { cnpj: {"saved_at": iso, "data": {...}} }
_CNPJA_MEM_CACHE: Dict[str, Dict[str, Any]] = {}
_CNPJA_LOCK = RLock()

def _cache_path_for(cnpj_num: str) -> str:
    return os.path.join(CNPJA_CACHE_DIR, f"{cnpj_num}.json")

def _load_disk_cache(cnpj_num: str) -> Optional[Dict[str, Any]]:
    path = _cache_path_for(cnpj_num)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _save_disk_cache(cnpj_num: str, payload: Dict[str, Any]) -> None:
    path = _cache_path_for(cnpj_num)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)

def _is_fresh(saved_at_iso: str, ttl_hours: int) -> bool:
    try:
        saved_at = datetime.fromisoformat(saved_at_iso)
    except Exception:
        return False
    return datetime.utcnow() - saved_at < timedelta(hours=ttl_hours)

def call_cnpja_office_cached(
    cnpj_num: str,
    ttl_hours: int = 168,
    allow_stale_on_error: bool = True,
    cache_only: bool = False,
) -> Dict[str, Any]:
    """
    Busca dados do CNPJá com cache. Se a API estiver indisponível ou sem API key,
    e existir cache no disco, retorna o cache mesmo que esteja 'stale' (quando
    allow_stale_on_error=True). Use cache_only=True para rodar totalmente offline.
    """
    # 0) tenta memória (fresh)
    with _CNPJA_LOCK:
        mem = _CNPJA_MEM_CACHE.get(cnpj_num)
        if mem and _is_fresh(mem.get("saved_at", ""), ttl_hours):
            return mem["data"]

    # 1) tenta disco (fresh / stale)
    disk_envelope = _load_disk_cache(cnpj_num)  # {"saved_at":..., "data": {...}}
    disk_is_fresh = bool(disk_envelope and _is_fresh(disk_envelope.get("saved_at", ""), ttl_hours))

    if cache_only:
        if disk_envelope:
            # reidrata memória e retorna (fresh ou stale)
            with _CNPJA_LOCK:
                _CNPJA_MEM_CACHE[cnpj_num] = disk_envelope
            return disk_envelope["data"]
        raise HTTPException(status_code=404, detail="Sem cache disponível para este CNPJ (modo cache_only).")

    # 2) se há disco fresh, usa e hidrata memória
    if disk_is_fresh:
        with _CNPJA_LOCK:
            _CNPJA_MEM_CACHE[cnpj_num] = disk_envelope
        return disk_envelope["data"]

    # 3) tenta request real (só se não é cache_only e não há fresh)
    load_dotenv(ENV_PATH)
    api_key = os.getenv("CNPJA_API_KEY")

    # se não tem chave e há cache (mesmo stale), devolve cache
    if not api_key:
        if allow_stale_on_error and disk_envelope:
            with _CNPJA_LOCK:
                _CNPJA_MEM_CACHE[cnpj_num] = disk_envelope
            return disk_envelope["data"]
        raise HTTPException(status_code=500, detail="CNPJA_API_KEY não configurada no .env e sem cache utilizável.")

    base_url = f"https://api.cnpja.com/office/{cnpj_num}"
    params = {
        "geocoding": "true",
        "simples": "true",
        "simplesHistory": "true",
        "registrations": "BR",
        "suframa": "true",
        "strategy": "CACHE_IF_FRESH",
        "maxAge": "7",
    }
    headers = {
        # Se sua conta exigir 'Bearer', troque para f"Bearer {api_key}"
        "Authorization": api_key,
        "Accept": "application/json",
        "User-Agent": "youon-internal/1.0",
        "Connection": "close",
    }

    try:
        resp = requests.get(base_url, headers=headers, params=params, timeout=TIMEOUT)
        if resp.status_code == 401:
            # se não autorizado e há cache, devolve cache stale
            if allow_stale_on_error and disk_envelope:
                with _CNPJA_LOCK:
                    _CNPJA_MEM_CACHE[cnpj_num] = disk_envelope
                return disk_envelope["data"]
            raise HTTPException(status_code=401, detail="Não autorizado na CNPJá (verifique a chave).")
        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail="CNPJ não encontrado na CNPJá.")
        if resp.status_code == 429:
            if allow_stale_on_error and disk_envelope:
                with _CNPJA_LOCK:
                    _CNPJA_MEM_CACHE[cnpj_num] = disk_envelope
                return disk_envelope["data"]
            raise HTTPException(status_code=429, detail="Rate limit da CNPJá atingido.")
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        # Fallback para cache stale se existir
        if allow_stale_on_error and disk_envelope:
            with _CNPJA_LOCK:
                _CNPJA_MEM_CACHE[cnpj_num] = disk_envelope
            return disk_envelope["data"]
        raise HTTPException(status_code=502, detail=f"Erro chamando CNPJá: {e}")

    # sucesso: persiste em memória + disco
    envelope = {"saved_at": datetime.utcnow().isoformat(timespec="seconds"), "data": data}
    with _CNPJA_LOCK:
        _CNPJA_MEM_CACHE[cnpj_num] = envelope
    _save_disk_cache(cnpj_num, envelope)
    return data
# ──────────────────────────────────────────────────────────────────────────────
# Modelos de resposta
# ──────────────────────────────────────────────────────────────────────────────
class PageResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list

class ConsultaResponse(BaseModel):
    cnpj: str
    geocoding: dict
    prefixos: dict
    parametros: dict
    resultados_count: int
    resultados_rows: list
    cnpja: Dict[str, Any]

@app.on_event("startup")
def _startup():
    global DATA_JOIN_GLOBAL, META
    DATA_JOIN_GLOBAL, META = build_global_index()

@app.get("/sources")
def sources():
    """Lista os GDBs descobertos e linhas indexadas por origem."""
    return META.get("sources", [])

@app.post("/reload")
def reload_index():
    """Força reindexação (ex.: após baixar nova pasta da concessionária)."""
    global DATA_JOIN_GLOBAL, META
    DATA_JOIN_GLOBAL, META = build_global_index()
    return {"status": "reloaded", "rows": META.get("rows"), "gdb_count": META.get("gdb_count")}

@app.get("/health")
def health():
    return {"status": "ok", **{k: v for k, v in META.items() if k != "sources"}}

@app.get("/schema")
def schema():
    return {"columns": META.get("columns", []), "rows": META.get("rows", 0)}

@app.get("/join", response_model=PageResponse)
def get_join(
    limit: int = Query(50, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    select: Optional[str] = Query(None),
    estado: Optional[str] = Query(None, description="Filtra por sigla de estado (ex.: SP, RJ)"),
    concessionaria: Optional[str] = Query(None, description="Filtra por concessionária (ex.: Enel, CPFL)"),
    ano: Optional[str] = Query(None, description="Filtra por ano inferido do GDB (ex.: 2024)"),
):
    if DATA_JOIN_GLOBAL is None:
        raise HTTPException(status_code=503, detail="Dataset ainda não carregado.")
    df = DATA_JOIN_GLOBAL

    if estado:
        df = df[df["src_estado"].astype(str).str.upper() == estado.upper()]
    if concessionaria:
        df = df[df["src_concessionaria"].astype(str).str.contains(concessionaria, case=False, na=False)]
    if ano:
        df = df[df["src_ano"].astype(str) == str(ano)]

    if select:
        cols = [c.strip() for c in select.split(",") if c.strip()]
        invalid = [c for c in cols if c not in df.columns]
        if invalid:
            raise HTTPException(status_code=400, detail=f"Colunas inválidas: {invalid}")
        df = df[cols]

    total = len(df)
    page = df.iloc[offset:offset+limit].to_dict(orient="records")
    return PageResponse(total=total, limit=limit, offset=offset, items=page)

# ──────────────────────────────────────────────────────────────────────────────
# Endpoint principal (CNPJ → geocoding → prefixo → filtro no índice global)
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/v1/consulta/{cnpj}", response_model=ConsultaResponse)
def consulta_por_cnpj(
    cnpj: str,
    decimals: int = Query(DEFAULT_DECIMALS, ge=1, le=6),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    select: Optional[str] = Query(None),
    estado: Optional[str] = Query(None, description="Opcional: restringe busca a um estado"),
    concessionaria: Optional[str] = Query(None, description="Opcional: restringe a uma concessionária"),
    ano: Optional[str] = Query(None, description="Opcional: restringe a um ano"),
):
    if DATA_JOIN_GLOBAL is None:
        raise HTTPException(status_code=503, detail="Dataset ainda não carregado.")

    cnpj_num = re.sub(r"\D", "", cnpj or "")
    if len(cnpj_num) != 14:
        raise HTTPException(status_code=400, detail="CNPJ inválido. Envie 14 dígitos.")

    data = call_cnpja_office_cached(cnpj_num)
    cnpja_sel = build_cnpja_selection(data)
    lat = first_of(data, ["address.latitude"])
    lon = first_of(data, ["address.longitude"])

    if lat in ("", None) or lon in ("", None):
        return ConsultaResponse(
            cnpj=cnpj_num,
            geocoding={"latitude": lat, "longitude": lon},
            prefixos={"lat": None, "lon": None},
            parametros={"decimals": decimals, "limit": limit, "offset": offset,
                        "estado": estado, "concessionaria": concessionaria, "ano": ano},
            resultados_count=0, resultados_rows=[],
            cnpja=cnpja_sel,
        )

    lat_pfx = latlon_prefix(lat, decimals)
    lon_pfx = latlon_prefix(lon, decimals)
    if not lat_pfx or not lon_pfx:
        raise HTTPException(status_code=500, detail="Falha ao formar prefixos de lat/lon.")

    df = DATA_JOIN_GLOBAL

    # filtros de origem opcionais
    if estado:
        df = df[df["src_estado"].astype(str).str.upper() == estado.upper()]
    if concessionaria:
        df = df[df["src_concessionaria"].astype(str).str.contains(concessionaria, case=False, na=False)]
    if ano:
        df = df[df["src_ano"].astype(str) == str(ano)]

    if "lat" not in df.columns or "lon" not in df.columns:
        raise HTTPException(status_code=500, detail="Dataset não possui colunas 'lat'/'lon'.")

    lat_fmt = df["lat"].map(lambda v: f"{float(v):.{decimals}f}" if pd.notna(v) else None)
    lon_fmt = df["lon"].map(lambda v: f"{float(v):.{decimals}f}" if pd.notna(v) else None)

    mask = (lat_fmt == lat_pfx) & (lon_fmt == lon_pfx)
    df = df[mask]
    total = len(df)

    if select:
        cols = [c.strip() for c in select.split(",") if c.strip()]
        invalid = [c for c in cols if c not in df.columns]
        if invalid:
            raise HTTPException(status_code=400, detail=f"Colunas inválidas: {invalid}")
        df = df[cols]

    page = df.iloc[offset:offset+limit].to_dict(orient="records")

    return ConsultaResponse(
        cnpj=cnpj_num,
        geocoding={"latitude": lat, "longitude": lon},
        prefixos={"lat": lat_pfx, "lon": lon_pfx},
        parametros={"decimals": decimals, "limit": limit, "offset": offset,
                    "estado": estado, "concessionaria": concessionaria, "ano": ano},
        resultados_count=total,
        resultados_rows=page,
        cnpja=cnpja_sel,
    )

# ──────────────────────────────────────────────────────────────────────────────
# Histórico CNPJá
# ──────────────────────────────────────────────────────────────────────────────
class HistoryItem(BaseModel):
    cnpj: str
    saved_at_disk: Optional[str] = None
    saved_at_memory: Optional[str] = None
    age_hours: Optional[float] = None
    has_disk: bool = False
    has_memory: bool = False
    razao_social: Optional[str] = None
    nome_fantasia: Optional[str] = None
    cidade: Optional[str] = None
    uf: Optional[str] = None

class HistoryListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[HistoryItem]

def _iter_disk_cache():
    if not os.path.isdir(CNPJA_CACHE_DIR):
        return
    for name in os.listdir(CNPJA_CACHE_DIR):
        if name.endswith(".json"):
            cnpj = name[:-5]
            payload = _load_disk_cache(cnpj)
            if payload:
                yield cnpj, payload

def _extract_company_fields(doc: dict) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    razao = first_of(doc, ["company.name", "legalName", "name"], None)
    fantasia = first_of(doc, ["alias", "company.alias", "tradeName", "nomeFantasia"], None)
    cidade = first_of(doc, ["address.city"], None)
    uf = first_of(doc, ["address.state"], None)
    return razao, fantasia, cidade, uf

def _age_hours(saved_at_iso: Optional[str]) -> Optional[float]:
    if not saved_at_iso:
        return None
    try:
        dt = datetime.fromisoformat(saved_at_iso)
        return round((datetime.utcnow() - dt).total_seconds() / 3600.0, 2)
    except Exception:
        return None

@app.get("/history", response_model=HistoryListResponse)
def history_list(
    q: Optional[str] = Query(None, description="Filtro por CNPJ ou razão/nome fantasia (case-insensitive)"),
    order: str = Query("desc", pattern="^(asc|desc)$", description="Ordenação por saved_at (disk/memory)"),
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    items: dict[str, HistoryItem] = {}

    for cnpj, envelope in _iter_disk_cache():
        saved_disk = envelope.get("saved_at")
        data = envelope.get("data", {})
        razao, fantasia, cidade, uf = _extract_company_fields(data)
        items[cnpj] = HistoryItem(
            cnpj=cnpj,
            saved_at_disk=saved_disk,
            age_hours=_age_hours(saved_disk),
            has_disk=True,
            razao_social=razao,
            nome_fantasia=fantasia,
            cidade=cidade,
            uf=uf,
        )

    with _CNPJA_LOCK:
        for cnpj, mem in _CNPJA_MEM_CACHE.items():
            saved_mem = mem.get("saved_at")
            data = mem.get("data", {})
            razao, fantasia, cidade, uf = _extract_company_fields(data)
            if cnpj in items:
                it = items[cnpj]
                it.saved_at_memory = saved_mem
                it.has_memory = True
                it.razao_social = it.razao_social or razao
                it.nome_fantasia = it.nome_fantasia or fantasia
                it.cidade = it.cidade or cidade
                it.uf = it.uf or uf
            else:
                items[cnpj] = HistoryItem(
                    cnpj=cnpj,
                    saved_at_memory=saved_mem,
                    age_hours=_age_hours(saved_mem),
                    has_memory=True,
                    razao_social=razao,
                    nome_fantasia=fantasia,
                    cidade=cidade,
                    uf=uf,
                )

    rows = list(items.values())

    # filtro q
    if q:
        ql = q.lower()
        def _match(h: HistoryItem) -> bool:
            if ql in h.cnpj:
                return True
            for field in (h.razao_social, h.nome_fantasia, h.cidade, h.uf):
                if field and ql in field.lower():
                    return True
            return False
        rows = [r for r in rows if _match(r)]

    # ordenação
    def _key(h: HistoryItem):
        base = h.saved_at_disk or h.saved_at_memory or ""
        return base
    rows.sort(key=_key, reverse=(order == "desc"))

    total = len(rows)
    page = rows[offset: offset + limit]
    return HistoryListResponse(total=total, limit=limit, offset=offset, items=page)

@app.get("/history/{cnpj}")
def history_detail(cnpj: str):
    cnpj_num = re.sub(r"\D", "", cnpj or "")
    out: dict[str, Any] = {"cnpj": cnpj_num}

    disk = _load_disk_cache(cnpj_num)
    if disk:
        out["disk"] = {"saved_at": disk.get("saved_at"), "data": disk.get("data")}

    with _CNPJA_LOCK:
        mem = _CNPJA_MEM_CACHE.get(cnpj_num)
        if mem:
            out["memory"] = mem

    if "disk" not in out and "memory" not in out:
        raise HTTPException(status_code=404, detail="CNPJ não encontrado no histórico.")
    return out

@app.delete("/history/{cnpj}")
def history_delete_one(cnpj: str):
    cnpj_num = re.sub(r"\D", "", cnpj or "")
    removed = {"memory": False, "disk": False}
    with _CNPJA_LOCK:
        if cnpj_num in _CNPJA_MEM_CACHE:
            _CNPJA_MEM_CACHE.pop(cnpj_num, None)
            removed["memory"] = True
    path = _cache_path_for(cnpj_num)
    if os.path.exists(path):
        os.remove(path)
        removed["disk"] = True
    return {"cnpj": cnpj_num, "removed": removed}

@app.delete("/history")
def history_delete_all():
    with _CNPJA_LOCK:
        _CNPJA_MEM_CACHE.clear()

    count_disk = 0
    if os.path.isdir(CNPJA_CACHE_DIR):
        for name in os.listdir(CNPJA_CACHE_DIR):
            if name.endswith(".json"):
                try:
                    os.remove(os.path.join(CNPJA_CACHE_DIR, name))
                    count_disk += 1
                except Exception:
                    pass
    return {"removed_memory": "all", "removed_disk_files": count_disk}
