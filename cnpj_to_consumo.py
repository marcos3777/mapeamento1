# arquivo: cnpj_to_consumo.py
# -*- coding: utf-8 -*-
import os
import re
import sys
import pathlib
import requests
import pandas as pd
from sqlalchemy import create_engine, text, bindparam
from dotenv import load_dotenv

# =====================
# Config
# =====================
TIMEOUT = 30
ENV_PATH = r"C:\Temp\admin\api\.env"
PRECISION_DECIMALS = 3            # casas decimais para prefix match (~111 m em 0.001º)
LIMIT_LB_PN = 200                 # linhas máximas no SELECT de visualização
OUT_DIR = pathlib.Path("_out")
OUT_DIR.mkdir(exist_ok=True)

# =====================
# Helpers
# =====================
def first_of(data, paths, default=""):
    for path in paths:
        cur = data
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

def join_nonempty(parts, sep=", "):
    return sep.join([str(p) for p in parts if p not in (None, "", [], {})])

def latlon_prefix(value, decimals=PRECISION_DECIMALS):
    if value is None:
        return None
    try:
        return f"{float(value):.{decimals}f}"
    except Exception:
        return None

def make_engine_from_env(db_url: str):
    """
    Usa psycopg3 (psycopg) por padrão; se não houver, tenta pg8000.
    Também converte asyncpg -> psycopg e ssl=require -> sslmode=require.
    Define search_path=intel_lead via options (sem precisar de SET search_path).
    """
    url = db_url.replace("+asyncpg", "+psycopg").replace("ssl=require", "sslmode=require")

    try:
        import psycopg  # noqa: F401
        chosen = "psycopg"
    except ImportError:
        try:
            import pg8000  # noqa: F401
            url = url.replace("+psycopg", "+pg8000")
            chosen = "pg8000"
        except ImportError as e:
            raise RuntimeError("Nenhum driver Postgres disponível. Instale 'psycopg[binary]' ou 'pg8000'.") from e

    # define search_path=intel_lead para toda conexão
    engine = create_engine(
        url,
        pool_pre_ping=True,
        connect_args={"options": "-c search_path=intel_lead"}
    )

    # teste rápido
    with engine.connect() as conn:
        conn.exec_driver_sql("SELECT 1")

    print(f"[DB] Conectado via {chosen}")
    return engine


def select_lb_pn_by_prefix(engine, lat_prefix: str, lon_prefix: str, limit=LIMIT_LB_PN):
    """
    Visualiza o join lb + pn filtrando por prefixo de lat/lon em ponto_notavel.
    """
    sql = text("""
        SELECT
            lb.*,
            pn.latitude  AS pn_latitude,
            pn.longitude AS pn_longitude,
            pn.pn_id     AS pn_pn_id
        FROM lead_bruto lb
        JOIN ponto_notavel pn ON lb.pn_con = pn.pn_id
        WHERE CAST(pn.latitude  AS TEXT) LIKE :lat_like
          AND CAST(pn.longitude AS TEXT) LIKE :lon_like
        ORDER BY pn.pn_id
        LIMIT :limit;
    """)
    with engine.begin() as conn:
        df = pd.read_sql(
            sql, conn,
            params={"lat_like": f"{lat_prefix}%", "lon_like": f"{lon_prefix}%", "limit": int(limit)}
        )
    return df


def update_lb_latlon_from_pn(engine):
    """
    Atualiza latitude/longitude em lead_bruto a partir de ponto_notavel
    quando latitude/longitude de lead_bruto estiverem NULL.
    Join pela regra: lb.pn_con = pn.pn_id.
    Retorna o número de linhas atualizadas.
    """
    sql = text("""
        WITH atualiza AS (
            UPDATE lead_bruto lb
               SET latitude  = pn.latitude,
                   longitude = pn.longitude
              FROM ponto_notavel pn
             WHERE lb.pn_con = pn.pn_id
               AND (lb.latitude IS NULL OR lb.longitude IS NULL)
         RETURNING 1
        )
        SELECT COUNT(*) AS linhas FROM atualiza;
    """)
    with engine.begin() as conn:
        res = conn.execute(sql).mappings().first()
        return int(res["linhas"] if res and "linhas" in res else 0)


def select_dic_fic_media_by_lb_ids(engine, lb_ids, exigir_12_meses=True):
    """
    Calcula DIC/FIC médios por lead (lb.id) usando lead_qualidade_mensal,
    para um conjunto de IDs. Retorna DataFrame com:
      id, meses_com_dados, dic_med, fic_med
    """
    if not lb_ids:
        return pd.DataFrame(columns=["id", "meses_com_dados", "dic_med", "fic_med"])

    base_sql = """
        SELECT
          lb.id,
          COUNT(DISTINCT lqm.mes)                       AS meses_com_dados,
          ROUND(AVG(lqm.dic::numeric), 3)               AS dic_med,
          ROUND(AVG(lqm.fic::numeric), 3)               AS fic_med
        FROM lead_bruto lb
        JOIN lead_qualidade_mensal lqm
          ON lqm.lead_bruto_id = lb.id
        WHERE lqm.mes BETWEEN 1 AND 12
          AND lb.id IN :ids
        GROUP BY lb.id
    """
    if exigir_12_meses:
        base_sql += "\nHAVING COUNT(DISTINCT lqm.mes) = 12"

    stmt = text(base_sql).bindparams(bindparam("ids", expanding=True))

    with engine.begin() as conn:
        df = pd.read_sql(stmt, conn, params={"ids": list(lb_ids)})
    return df


def print_basic_company_block(data):
    razao       = first_of(data, ["company.name", "legalName", "name"])
    alias       = first_of(data, ["alias", "company.alias", "tradeName", "nomeFantasia"])
    cnpj_out    = first_of(data, ["taxId", "company.taxId"])
    status_txt  = first_of(data, ["status.text", "company.status.text", "status"])
    status_date = first_of(data, ["statusDate"])
    founded     = first_of(data, ["founded", "company.opening", "opening", "company.founded"])
    updated     = first_of(data, ["updated"])

    street      = first_of(data, ["address.street"])
    number      = first_of(data, ["address.number"])
    district    = first_of(data, ["address.district"])
    city        = first_of(data, ["address.city"])
    state       = first_of(data, ["address.state"])
    zipc        = first_of(data, ["address.zip"])
    details     = first_of(data, ["address.details"])
    lat         = first_of(data, ["address.latitude"])
    lon         = first_of(data, ["address.longitude"])

    print("\n=== EMPRESA ===")
    print(f"Razão social : {razao}")
    print(f"Nome fantasia: {alias}")
    print(f"CNPJ         : {cnpj_out}")
    print(f"Situação     : {status_txt} (desde {status_date or '-'})")
    print(f"Abertura     : {founded}")
    print(f"Atualizado em: {updated}")

    print("\n=== ENDEREÇO (API) ===")
    endereco = join_nonempty([street, number, district, city, state, zipc])
    print(f"Endereço: {endereco or '-'}")
    if details:
        print(f"Complemento: {details}")
    if lat and lon:
        print(f"Geo (API): lat={lat} • lon={lon}")

# =====================
# Main
# =====================
def main():
    if len(sys.argv) < 2:
        print("Uso: python cnpj_to_consumo.py <CNPJ> [--update]")
        sys.exit(1)

    args = sys.argv[1:]
    cnpj_input = args[0]
    do_update = "--update" in args

    cnpj = re.sub(r"\D", "", cnpj_input)
    if len(cnpj) != 14:
        print("CNPJ inválido. Informe 14 dígitos (com ou sem máscara).")
        sys.exit(1)

    # ENV
    load_dotenv(ENV_PATH)
    api_key = os.getenv("CNPJA_API_KEY")
    if not api_key:
        print("Erro: defina CNPJA_API_KEY no seu .env")
        sys.exit(1)

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("Erro: defina DATABASE_URL no seu .env")
        sys.exit(1)

    # 1) Consulta CNPJá (com geocoding)
    base_url = f"https://api.cnpja.com/office/{cnpj}"
    params = {"geocoding": "true", "strategy": "CACHE_IF_FRESH", "maxAge": "7"}
    headers = {
        "Authorization": api_key,
        "Accept": "application/json",
        "User-Agent": "youon-internal/1.0",
        "Connection": "close",
    }

    try:
        resp = requests.get(base_url, headers=headers, params=params, timeout=TIMEOUT)
        if resp.status_code == 401:
            print("Não autorizado: verifique sua CNPJA_API_KEY."); sys.exit(1)
        if resp.status_code == 404:
            print("CNPJ não encontrado (fora do cache/escopo do plano?)."); sys.exit(1)
        if resp.status_code == 429:
            print("Limite de uso excedido (rate limit)."); sys.exit(1)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        print(f"Erro de requisição: {e}")
        sys.exit(1)

    print_basic_company_block(data)

    lat = first_of(data, ["address.latitude"])
    lon = first_of(data, ["address.longitude"])
    if lat in ("", None) or lon in ("", None):
        print("\n[!] A API não retornou latitude/longitude. Ative 'geocoding' no plano, ou geocodifique o endereço por fora.")
        sys.exit(0)

    lat_pfx = latlon_prefix(lat, PRECISION_DECIMALS)
    lon_pfx = latlon_prefix(lon, PRECISION_DECIMALS)
    if not lat_pfx or not lon_pfx:
        print("\n[!] Não foi possível formar prefixo de lat/lon.")
        sys.exit(0)

    print(f"\n=== BUSCA NO BANCO por prefixo ({PRECISION_DECIMALS} casas) ===")
    print(f"lat LIKE '{lat_pfx}%'  •  lon LIKE '{lon_pfx}%'")

    # 2) Conexão DB
    engine = make_engine_from_env(db_url)

    # 3) (Opcional) Atualização de latitude/longitude em lead_bruto a partir do join
    if do_update:
        try:
            linhas = update_lb_latlon_from_pn(engine)
            print(f"\n[UPDATE] lead_bruto atualizado com lat/lon de ponto_notavel: {linhas} linha(s).")
        except Exception as e:
            print(f"\n[UPDATE] Falhou ao atualizar lead_bruto: {e}")

    # 4) Visualização: join lead_bruto + ponto_notavel filtrado por prefixo
    try:
        df_lbpn = select_lb_pn_by_prefix(engine, lat_pfx, lon_pfx, limit=LIMIT_LB_PN)
        print(f"\n[SELECT lb + pn] Linhas: {len(df_lbpn)}")
        if not df_lbpn.empty:
            print(df_lbpn.head(10))
            out_csv = OUT_DIR / f"lb_pn_{cnpj}.csv"
            df_lbpn.to_csv(out_csv, index=False, encoding="utf-8-sig")
            print(f"→ Salvo em: {out_csv}")

            # 5) === NOVO: médias de DIC/FIC por lead encontrado ===
            lb_ids = list({row for row in df_lbpn["id"].dropna().astype(str).tolist()})
            df_dicfic = select_dic_fic_media_by_lb_ids(engine, lb_ids, exigir_12_meses=True)
            print(f"\n[MÉDIA DIC/FIC] Leads com 12 meses: {len(df_dicfic)}")
            if not df_dicfic.empty:
                print(df_dicfic.head(20))
                out_csv2 = OUT_DIR / f"dicfic_media_{cnpj}.csv"
                df_dicfic.to_csv(out_csv2, index=False, encoding="utf-8-sig")
                print(f"→ Salvo em: {out_csv2}")
            else:
                print("Nenhum lead com 12 meses completos de DIC/FIC para esses IDs.")
        else:
            print("Nenhum registro encontrado para esse prefixo.")
    except Exception as e:
        print(f"\n[Erro no SELECT lb + pn]: {e}")

    print("\nConcluído.\n")

if __name__ == "__main__":
    main()
