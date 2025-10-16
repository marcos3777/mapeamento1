# ponnot_prefix_lookup.py
# Uso:
#   python ponnot_prefix_lookup.py --gdb "downloads/Enel_SP_2024.gdb" --lat -23.6351697 --lon -46.6420449 --decimals 3 --show 30
#
# Saída: imprime quantos registros bateu e lista as colunas chave (COD_ID, lat, lon); use --select para listar mais colunas.

import argparse
import os
import fiona
import pandas as pd
import geopandas as gpd

def find_layer(name_contains: str, layers: list[str]) -> str:
    key = name_contains.lower()
    for L in layers:
        if key in L.lower():
            return L
    raise SystemExit(f"Não achei layer contendo '{name_contains}'. Layers disponíveis: {layers}")

def ensure_wgs84(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if gdf.crs is None:
        # Se souber o CRS original, ajuste aqui (exemplo):
        # gdf = gdf.set_crs(31983)  # SIRGAS/UTM 23S (exemplo)
        raise SystemExit("Layer sem CRS; defina o CRS correto (set_crs) antes do reprojeto.")
    if gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)
    return gdf

def latlon_prefix(value, decimals: int):
    if pd.isna(value):
        return None
    try:
        return f"{float(value):.{decimals}f}"
    except Exception:
        return None

def detect_cod_id_col(df: pd.DataFrame) -> str | None:
    for c in df.columns:
        if c.lower() == "cod_id":
            return c
    return None

def main():
    ap = argparse.ArgumentParser(
        description="Busca na PONNOT por prefixo de latitude/longitude (string match)."
    )
    ap.add_argument("--gdb", required=True, help="Caminho do arquivo .gdb (ex.: downloads/Enel_SP_2024.gdb)")
    ap.add_argument("--lat", required=True, type=float, help="Latitude (graus)")
    ap.add_argument("--lon", required=True, type=float, help="Longitude (graus)")
    ap.add_argument("--decimals", type=int, default=3, help="Casas decimais do prefixo (default=3)")
    ap.add_argument("--show", type=int, default=20, help="Quantos registros imprimir (default=20)")
    ap.add_argument("--select", type=str, default="", help="Colunas extras separadas por vírgula para exibir")
    args = ap.parse_args()

    gdb_path = args.gdb
    if not os.path.exists(gdb_path):
        raise SystemExit(f"GDB não encontrado: {gdb_path}")

    layers = fiona.listlayers(gdb_path)
    ponnot_layer = find_layer("PONNOT", layers)
    print(f"[INFO] Lendo layer: {ponnot_layer}")

    gdf = gpd.read_file(gdb_path, layer=ponnot_layer)
    gdf = ensure_wgs84(gdf)

    # Se não for ponto, usa centróide (apenas para depuração rápida)
    if not gdf.geom_type.isin(["Point"]).all():
        gdf["geometry"] = gdf.geometry.centroid

    gdf["lon"] = gdf.geometry.x
    gdf["lat"] = gdf.geometry.y

    # prefixos-alvo
    target_lat_pfx = f"{float(args.lat):.{args.decimals}f}"
    target_lon_pfx = f"{float(args.lon):.{args.decimals}f}"
    print(f"[INFO] Prefixos alvo -> lat_pfx={target_lat_pfx} | lon_pfx={target_lon_pfx}")

    # gera prefixos nas linhas
    lat_fmt = gdf["lat"].map(lambda v: latlon_prefix(v, args.decimals))
    lon_fmt = gdf["lon"].map(lambda v: latlon_prefix(v, args.decimals))
    mask = (lat_fmt == target_lat_pfx) & (lon_fmt == target_lon_pfx)

    hit = gdf.loc[mask].copy()
    total = len(hit)
    print(f"[RESULT] Encontrados {total} registro(s) em {ponnot_layer} com esses prefixos.")

    if total == 0:
        return

    # monta colunas para mostrar
    cod_id_col = detect_cod_id_col(hit)
    base_cols = ["lat", "lon"]
    if cod_id_col:
        base_cols.insert(0, cod_id_col)

    extra_cols = []
    if args.select.strip():
        extra_cols = [c.strip() for c in args.select.split(",") if c.strip() and c in hit.columns]

    cols_to_show = list(dict.fromkeys(base_cols + extra_cols))  # remove duplicatas mantendo ordem

    # imprime
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 200)
    print(hit[cols_to_show].head(args.show).to_string(index=False))

if __name__ == "__main__":
    main()
