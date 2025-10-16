# -*- coding: utf-8 -*-
"""
API para buscar um CEP em múltiplas camadas de um GDB e devolver um FeatureCollection (GeoJSON).
Requisitos:
  pip install fastapi uvicorn fiona geopandas shapely pyproj pandas
"""

import re
from typing import List, Dict, Any
import fiona
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

# ======== CONFIGURE AQUI ========
GDB_PATH = r"C:\Temp\admin\downloads\Enel_SP_2024.gdb"
LAYERS_TO_SEARCH: List[str] = [
    "SSDAT","SSDBT","SSDMT","PONNOT",
    "UNCRAT","UNCRBT","UNCRMT","UNREAT","UNREMT",
    "UNSEAT","UNSEBT","UNSEMT","UNTRAT","UNTRMT",
    "ARAT","CONJ","SUB","BAR","BASE","BAY","BE",
    "CRVCRG","CTAT","CTMT","EP","EQCR","EQME","EQRE",
    "EQSE","EQTRAT","EQTRM","EQTRMT","PIP","PNT","PT",
    "RAMLIG","SEGCON","UCAT_tab","UCBT_tab","UCMT_tab",
    "UGAT_tab","UGBT_tab","UGMT_tab",
]
PREFERRED_PREVIEW = ["CEP", "COD_ID", "PN_CON", "MUNICIPIO", "BAIRRO", "CIDADE"]
# ================================

def only_digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")

app = FastAPI(title="Buscador CEP GDB")

# CORS liberado (ajuste para o domínio do seu front em produção)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/layers")
def list_layers():
    try:
        return {"layers": fiona.listlayers(GDB_PATH)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao abrir GDB: {e}")

@app.get("/search")
def search_cep(cep: str = Query(..., description="CEP com ou sem hífen")):
    """
    GET /search?cep=04826240
    Retorna FeatureCollection com um campo extra "__layer" indicando a camada de origem.
    """
    target_digits = only_digits(cep)
    if len(target_digits) != 8:
        raise HTTPException(status_code=400, detail="CEP inválido. Use 8 dígitos (com ou sem hífen).")

    # Verifica camadas disponíveis
    try:
        available = set(fiona.listlayers(GDB_PATH))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao abrir GDB: {e}")

    layers_plan = [lyr for lyr in LAYERS_TO_SEARCH if lyr in available]
    if not layers_plan:
        raise HTTPException(status_code=404, detail="Nenhuma das camadas configuradas existe no GDB.")

    features: List[Dict[str, Any]] = []
    meta_by_layer: Dict[str, Any] = {}

    for layer in layers_plan:
        try:
            with fiona.open(GDB_PATH, layer=layer) as src:
                prop_names = list(src.schema.get("properties", {}).keys())
                cep_col = None
                if "CEP" in prop_names:
                    cep_col = "CEP"
                else:
                    for p in prop_names:
                        if p.upper() == "CEP":
                            cep_col = p
                            break

                if not cep_col:
                    continue  # pula camadas sem CEP

                count_layer = 0
                for feat in src:
                    props = feat.get("properties", {})
                    cep_val = props.get(cep_col)
                    if cep_val is None:
                        continue
                    if only_digits(str(cep_val)) == target_digits:
                        # anexa marcador de camada
                        props["__layer"] = layer
                        # cria cópia limpa do feature
                        features.append({
                            "type": "Feature",
                            "geometry": feat.get("geometry"),
                            "properties": props
                        })
                        count_layer += 1

                if count_layer:
                    # metadados úteis pro front (ex: colunas preferidas presentes)
                    present_pref = [c for c in PREFERRED_PREVIEW if c in prop_names]
                    meta_by_layer[layer] = {
                        "matches": count_layer,
                        "preview_cols": ["__layer"] + present_pref
                    }
        except Exception as e:
            # registra erro de camada mas segue as demais
            meta_by_layer[layer] = {"error": str(e)}

    if not features:
        return {
            "query": {"cep": cep, "normalized": target_digits},
            "count": 0,
            "layers_checked": layers_plan,
            "meta": meta_by_layer,
            "type": "FeatureCollection",
            "features": []
        }

    return {
        "query": {"cep": cep, "normalized": target_digits},
        "count": len(features),
        "layers_checked": layers_plan,
        "meta": meta_by_layer,
        "type": "FeatureCollection",
        "features": features
    }

# Para rodar: uvicorn app:app --reload
