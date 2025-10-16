"""Exporta camadas UC* e UG* de um File Geodatabase para JSON bruto.

O script percorre as camadas especificadas (por padrão UCAT_tab, UCBT_tab,
UCMT_tab, UGAT_tab, UGBT_tab e UGMT_tab), converte o conteúdo completo de cada
uma para GeoJSON (em WGS84 quando possível) e salva os resultados em um único
arquivo JSON.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Dict, List

import fiona
import geopandas as gpd

DEFAULT_LAYERS: List[str] = [
    "UCAT_tab",
    "UCBT_tab",
    "UCMT_tab",
    "UGAT_tab",
    "UGBT_tab",
    "UGMT_tab",
]


def ensure_wgs84(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Retorna o GeoDataFrame convertido para WGS84 quando possível."""

    if gdf.crs is None:
        return gdf
    try:
        epsg = gdf.crs.to_epsg()
    except Exception:
        epsg = None
    if epsg == 4326:
        return gdf
    try:
        return gdf.to_crs(epsg=4326)
    except Exception:
        return gdf


def load_layer(gdb_path: str, layer: str) -> List[dict]:
    """Carrega uma camada do GDB e retorna a lista de features em formato GeoJSON."""

    gdf = gpd.read_file(gdb_path, layer=layer)
    gdf = ensure_wgs84(gdf)
    geojson_dict = json.loads(gdf.to_json())
    return geojson_dict.get("features", [])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Exporta camadas UC/UG para JSON bruto (GeoJSON por camada)."
    )
    parser.add_argument("gdb", help="Caminho para o arquivo .gdb")
    parser.add_argument(
        "--out",
        default="camadas_uc_ug.json",
        help="Arquivo de saída (JSON). Default: camadas_uc_ug.json",
    )
    parser.add_argument(
        "--layers",
        nargs="*",
        default=None,
        help="Lista de camadas específicas a processar. Default usa as camadas UC/UG",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    gdb_path = args.gdb
    if not os.path.exists(gdb_path):
        parser.error(f"GDB não encontrado: {gdb_path}")

    try:
        available_layers = set(fiona.listlayers(gdb_path))
    except Exception as exc:  # pragma: no cover - CLI helper
        raise SystemExit(f"Falha ao listar camadas do GDB: {exc}")

    desired_layers = args.layers or DEFAULT_LAYERS
    layers_plan = [layer for layer in desired_layers if layer in available_layers]
    if not layers_plan:
        raise SystemExit("Nenhuma das camadas solicitadas está presente no GDB.")

    missing = [layer for layer in desired_layers if layer not in available_layers]
    if missing:
        print("⚠️  Ignorando camadas ausentes:", ", ".join(sorted(missing)))

    result: Dict[str, List[dict]] = {}
    for layer in layers_plan:
        try:
            features = load_layer(gdb_path, layer)
        except Exception as exc:
            print(f"⚠️  Erro ao ler camada {layer}: {exc}")
            continue

        result[layer] = features
        print(f"📦 {layer}: {len(features)} registro(s) exportados.")

    if not result:
        raise SystemExit("Nenhuma camada pôde ser exportada.")

    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fp:
        json.dump(result, fp, ensure_ascii=False)
    print(f"🗃️  JSON completo salvo em: {out_path}")


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    main()
