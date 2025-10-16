# -*- coding: utf-8 -*-
"""Ferramentas para exportar camadas contendo CEP de um File Geodatabase.

O script percorre as camadas configuradas, identifica automaticamente a
coluna de CEP (aceitando variações de caixa), e oferece duas exportações:

1. **GeoJSON filtrado por CEP** – basta informar o CEP desejado (com ou sem
   hífen) via ``--cep`` que o script consolida todos os registros
   correspondentes em um único arquivo GeoJSON.
2. **JSON completo** – independente do filtro de CEP, todas as camadas são
   serializadas integralmente (com geometria em WGS84) em um documento JSON,
   agrupadas por nome de camada.

Requisitos principais: ``fiona`` e ``geopandas``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from typing import Dict, Iterable, List, Optional

import fiona
import geopandas as gpd


LAYERS_TO_SEARCH: List[str] = [
    "SSDAT",
    "SSDBT",
    "SSDMT",
    "PONNOT",
    "UNCRAT",
    "UNCRBT",
    "UNCRMT",
    "UNREAT",
    "UNREMT",
    "UNSEAT",
    "UNSEBT",
    "UNSEMT",
    "UNTRAT",
    "UNTRMT",
    "ARAT",
    "CONJ",
    "SUB",
    "BAR",
    "BASE",
    "BAY",
    "BE",
    "CRVCRG",
    "CTAT",
    "CTMT",
    "EP",
    "EQCR",
    "EQME",
    "EQRE",
    "EQSE",
    "EQTRAT",
    "EQTRM",
    "EQTRMT",
    "PIP",
    "PNT",
    "PT",
    "RAMLIG",
    "SEGCON",
    "UCAT_tab",
    "UCBT_tab",
    "UCMT_tab",
    "UGAT_tab",
    "UGBT_tab",
    "UGMT_tab",
]


def only_digits(value: Optional[str]) -> str:
    """Remove qualquer caractere não numérico."""

    return re.sub(r"\D", "", value or "")


def normalize_columns(columns: Iterable[str]) -> Dict[str, str]:
    """Mapeia nomes de colunas sem espaços extras e preserva o nome original."""

    mapping: Dict[str, str] = {}
    for col in columns:
        cleaned = col.strip()
        mapping[cleaned] = col
    return mapping


def find_cep_column(columns: Iterable[str]) -> Optional[str]:
    """Encontra coluna de CEP ignorando caixa."""

    for col in columns:
        if col.upper() == "CEP":
            return col
    return None


def ensure_wgs84(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Garante que o GeoDataFrame está em WGS84 quando possível."""

    if gdf.crs is None:
        return gdf
    if gdf.crs.to_epsg() == 4326:
        return gdf
    try:
        return gdf.to_crs(epsg=4326)
    except Exception:
        return gdf


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Exporta camadas com CEP de um File Geodatabase."
    )
    parser.add_argument("gdb", help="Caminho para o arquivo .gdb")
    parser.add_argument(
        "--cep",
        help="CEP alvo (com ou sem hífen). Quando informado gera GeoJSON filtrado",
    )
    parser.add_argument(
        "--out-dir",
        default=".",
        help="Diretório de saída para os arquivos gerados",
    )
    parser.add_argument(
        "--layers",
        nargs="*",
        default=None,
        help="Lista de camadas específicas a processar. Default usa LAYERS_TO_SEARCH",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    gdb_path = args.gdb
    if not os.path.exists(gdb_path):
        parser.error(f"GDB não encontrado: {gdb_path}")

    os.makedirs(args.out_dir, exist_ok=True)

    try:
        available_layers = set(fiona.listlayers(gdb_path))
    except Exception as exc:  # pragma: no cover - mensagem auxiliar para CLI
        raise SystemExit(f"Falha ao listar camadas do GDB: {exc}")

    desired_layers = args.layers or LAYERS_TO_SEARCH
    layers_plan = [layer for layer in desired_layers if layer in available_layers]
    if not layers_plan:
        raise SystemExit("Nenhuma das camadas configuradas foi encontrada no GDB.")

    missing = [layer for layer in desired_layers if layer not in available_layers]
    if missing:
        print("⚠️  Ignorando camadas ausentes:", ", ".join(sorted(missing)))

    cep_digits = only_digits(args.cep) if args.cep else None
    match_features: List[dict] = []
    full_layers: Dict[str, List[dict]] = {}

    for layer in layers_plan:
        try:
            gdf = gpd.read_file(gdb_path, layer=layer)
        except Exception as exc:
            print(f"⚠️  Erro ao ler camada {layer}: {exc}")
            continue

        # Normaliza cabeçalhos e preserva os dados na ordem original
        column_mapping = normalize_columns(gdf.columns)
        gdf = gdf.rename(columns={orig: clean for clean, orig in column_mapping.items()})

        cep_col = find_cep_column(gdf.columns)
        if not cep_col:
            print(f"⏭️  {layer}: sem coluna 'CEP' – camada ignorada.")
            continue

        gdf["__layer"] = layer
        gdf = ensure_wgs84(gdf)

        # Exportação completa da camada
        full_json = json.loads(gdf.to_json())
        full_layers[layer] = full_json.get("features", [])
        print(f"📦 {layer}: {len(full_layers[layer])} registro(s) exportados para JSON completo.")

        # Exportação filtrada por CEP (quando solicitado)
        if cep_digits:
            mask = gdf[cep_col].apply(lambda value: only_digits(str(value)) == cep_digits)
            matches = gdf.loc[mask]
            if not matches.empty:
                matches_json = json.loads(matches.to_json())
                features = matches_json.get("features", [])
                match_features.extend(features)
                print(f"✅ {layer}: {len(features)} registro(s) com CEP {args.cep}.")
            else:
                print(f"—  {layer}: nenhum registro encontrado com CEP {args.cep}.")

    # Salva GeoJSON filtrado
    if cep_digits and match_features:
        geojson_path = os.path.join(args.out_dir, f"cep_{cep_digits}_matches.geojson")
        collection = {"type": "FeatureCollection", "features": match_features}
        with open(geojson_path, "w", encoding="utf-8") as fp:
            json.dump(collection, fp, ensure_ascii=False)
        print(f"💾 GeoJSON consolidado salvo em: {os.path.abspath(geojson_path)}")
    elif cep_digits:
        print("🔍 Nenhum registro com esse CEP foi encontrado nas camadas verificadas.")

    # Salva JSON completo das camadas processadas
    if full_layers:
        json_path = os.path.join(args.out_dir, "camadas_com_cep.json")
        with open(json_path, "w", encoding="utf-8") as fp:
            json.dump(full_layers, fp, ensure_ascii=False)
        print(f"🗃️  JSON completo das camadas salvo em: {os.path.abspath(json_path)}")


if __name__ == "__main__":
    main()

