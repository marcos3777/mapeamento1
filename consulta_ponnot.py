# -*- coding: utf-8 -*-
"""
Script simples para buscar um COD_ID na camada PONNOT de todos os GDBs.

Uso:
    python consulta_ponnot_id.py <COD_ID>

Exemplo:
    python consulta_ponnot_id.py a2195b282000670300b3bf244f5133b810a4089f01a982b353798891bcbeac62
"""

import os
import sys
import glob
import geopandas as gpd
import fiona

# Caminho base (ajuste se necessário)
DOWNLOADS_ROOT = os.getenv(
    "DOWNLOADS_ROOT",
    r"C:\Temp\admin\downloads"
)

def discover_gdbs(root):
    return sorted(glob.glob(os.path.join(root, "**", "*.gdb"), recursive=True))

def find_layer(name_contains, layers):
    key = name_contains.lower()
    for L in layers:
        if key in L.lower():
            return L
    raise ValueError(f"Não achei layer contendo '{name_contains}'. Layers disponíveis: {layers}")

def main(ponnot_id):
    gdbs = discover_gdbs(DOWNLOADS_ROOT)
    encontrados = []

    for gdb in gdbs:
        try:
            layers = fiona.listlayers(gdb)
            layer = find_layer("PONNOT", layers)
            gdf = gpd.read_file(gdb, layer=layer)
            gdf.columns = [c.strip() for c in gdf.columns]

            col_cod = next((c for c in gdf.columns if c.lower() == "cod_id"), None)
            if not col_cod:
                continue

            match = gdf[gdf[col_cod].astype(str) == ponnot_id]
            if not match.empty:
                print(f"\n=== ENCONTRADO EM: {gdb} (layer: {layer}) ===")
                print(match)
                encontrados.append((gdb, layer, match))
        except Exception as e:
            print(f"[ERRO] Falha ao ler {gdb}: {e}")

    if not encontrados:
        print(f"Nenhum registro encontrado com COD_ID = {ponnot_id}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python consulta_ponnot_id.py <COD_ID>")
        sys.exit(1)
    cod_id = sys.argv[1]
    main(cod_id)
