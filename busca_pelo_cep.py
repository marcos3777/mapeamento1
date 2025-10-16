# -*- coding: utf-8 -*-
"""
Busca um CEP em múltiplas camadas de um File Geodatabase (.gdb), apenas nas que possuem a coluna CEP.
- Lista de camadas-alvo definida abaixo (layers_to_search)
- Normaliza CEP (com ou sem hífen) para comparação
- Mostra resumo por camada e salva um GeoJSON consolidado com os matches (em WGS84)

Requisitos:
pip install fiona geopandas shapely pyproj
"""

import re
import os
from typing import List
import fiona
import geopandas as gpd
import pandas as pd

# ======== CONFIGURE AQUI ========
gdb_path = r"C:\Temp\admin\downloads\Enel_SP_2024.gdb"
target_cep = "04826240"

layers_to_search: List[str] = [
    "SSDAT", "SSDBT", "SSDMT", "PONNOT",
    "UNCRAT", "UNCRBT", "UNCRMT", "UNREAT", "UNREMT",
    "UNSEAT", "UNSEBT", "UNSEMT", "UNTRAT", "UNTRMT",
    "ARAT", "CONJ", "SUB", "BAR", "BASE", "BAY", "BE",
    "CRVCRG", "CTAT", "CTMT", "EP", "EQCR", "EQME", "EQRE",
    "EQSE", "EQTRAT", "EQTRM", "EQTRMT", "PIP", "PNT", "PT",
    "RAMLIG", "SEGCON", "UCAT_tab", "UCBT_tab", "UCMT_tab",
    "UGAT_tab", "UGBT_tab", "UGMT_tab",
]
# ================================

def only_digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")

target_digits = only_digits(target_cep)

print(f"🔎 Procurando CEP '{target_cep}' (normalizado={target_digits}) no GDB:\n  {gdb_path}\n")

# Verifica camadas existentes no GDB
try:
    available_layers = set(fiona.listlayers(gdb_path))
except Exception as e:
    raise SystemExit(f"Erro ao abrir GDB: {e}")

# Interseção entre as pedidas e as realmente existentes
layers_plan = [lyr for lyr in layers_to_search if lyr in available_layers]
missing = [lyr for lyr in layers_to_search if lyr not in available_layers]

if missing:
    print("⚠️ Camadas não encontradas no GDB (ignoradas):", ", ".join(missing))
if not layers_plan:
    raise SystemExit("Nenhuma das camadas solicitadas existe no GDB.")

all_matches: List[gpd.GeoDataFrame] = []

for layer in layers_plan:
    try:
        with fiona.open(gdb_path, layer=layer) as src:
            # Procura coluna CEP (case-insensitive), mas exige nome == 'CEP' preferencialmente
            prop_names = list(src.schema.get("properties", {}).keys())
            cep_col = None
            # Prioriza 'CEP' exato; se não houver, aceita qualquer coluna cujo nome seja 'cep' case-insensitive
            if "CEP" in prop_names:
                cep_col = "CEP"
            else:
                for p in prop_names:
                    if p.upper() == "CEP":
                        cep_col = p
                        break

            if not cep_col:
                print(f"⏭️  {layer}: sem coluna 'CEP' — pulando.")
                continue

            # Itera registros e coleta matches
            layer_matches = []
            for feat in src:
                props = feat.get("properties", {})
                cep_val = props.get(cep_col)
                if cep_val is None:
                    continue
                if only_digits(str(cep_val)) == target_digits:
                    layer_matches.append(feat)

            if layer_matches:
                gdf = gpd.GeoDataFrame.from_features(layer_matches, crs=src.crs)
                gdf["__layer"] = layer

                # Tenta padronizar para WGS84
                try:
                    gdf = gdf.to_crs(epsg=4326)
                except Exception:
                    pass

                all_matches.append(gdf)

                # Mostra resumo rápido
                cols = gdf.columns.tolist()
                preferidas = [c for c in ["CEP", "COD_ID", "PN_CON", "MUNICIPIO", "BAIRRO", "CIDADE"] if c in cols]
                preview_cols = ["__layer"] + preferidas
                print(f"✅ {layer}: {len(gdf)} registro(s) com CEP {target_cep}")
                print(gdf[preview_cols].head().to_string(index=False))
            else:
                print(f"—  {layer}: nenhum registro com CEP {target_cep}")

    except Exception as e:
        print(f"⚠️ Erro ao ler {layer}: {e}")

# Consolida e salva
if all_matches:
    result = pd.concat(all_matches, ignore_index=True)
    out_name = f"cep_{target_digits}_matches.geojson"
    result.to_file(out_name, driver="GeoJSON")
    print(f"\n💾 Resultado consolidado salvo em: {os.path.abspath(out_name)}")
    print(f"📦 Total de matches: {len(result)} em {result['__layer'].nunique()} camada(s).")
else:
    print("\n🔍 Nenhum registro encontrado com esse CEP nas camadas verificadas.")
