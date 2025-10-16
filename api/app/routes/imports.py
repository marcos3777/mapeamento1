# app.py (ou onde está seu router)
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text, bindparam
from pathlib import Path
import os
import asyncio
from app.db import get_session
from app.config import UI_TO_DB, DB_SCHEMA
from fastapi import Query
from app.utils.downloader import download_many, _parse_obs, map_status_ui, safe_name
from app.schemas.imports import ImportRequest
router = APIRouter(prefix="/v1")  
from app.utils.inspector import _list_gdb_layers, _inspect_any_file, _HAS_FIONA, _inspect_directory, _inspect_zip, _is_zip_file, _zip_find_gdbs, _zip_find_shp_layers
DB_SCHEMA = "intel_lead"
UI_TO_DB = {}        
DOWNLOAD_BASE = Path(os.getenv(
    "DATA_DIR",
    r"C:\Users\RaphaelaOliveiraTatt\OneDrive - You On\Documentos\admin\data"
)).expanduser()

q_catalog = text(f"""
    WITH base AS (
      SELECT
        id,
        title,
        url,
        tags,
        -- Acha uma tag de data/ano: YYYY ou YYYY-MM-DD
        (
          SELECT t
          FROM unnest(tags) AS t
          WHERE t ~ '^[0-9]{{4}}(-[0-9]{{2}}-[0-9]{{2}})?$'
          ORDER BY length(t) DESC
          LIMIT 1
        ) AS tag_date,
        -- Acha uma tag "distribuidora" (exclui tags técnicas/comuns)
        (
          SELECT t
          FROM unnest(tags) AS t
          WHERE NOT (t ~ '^[0-9]{{4}}(-[0-9]{{2}}-[0-9]{{2}})?$')
            AND upper(t) NOT IN ('BDGD','SIG-R','DISTRIBUICAO','DISTRIBUIÇÃO','ANEEL','BDGD 2.0')
          ORDER BY length(t) DESC
          LIMIT 1
        ) AS tag_dist
      FROM {DB_SCHEMA}.dataset_url_catalog
    )
    SELECT
      id,
      title,
      url,
      tag_dist AS distribuidora,
      LEFT(tag_date, 4)::int AS ano
    FROM base
    WHERE tag_dist IS NOT NULL
      AND tag_date IS NOT NULL
      AND UPPER(tag_dist) IN :dists
      AND LEFT(tag_date, 4)::int IN :anos
    ORDER BY distribuidora, ano, id
""").bindparams(
    bindparam("dists", expanding=True),
    bindparam("anos",  expanding=True),
)


@router.post("/importar")
async def importar(req: ImportRequest, background: BackgroundTasks, db: AsyncSession = Depends(get_session)):
    dists_ui = req.distribuidoras or []
    anos = req.anos or []
    dists_db = [UI_TO_DB.get(d, d) for d in dists_ui]
    dists_upper =  [d.upper() for d in dists_db]
    if not dists_ui or not anos:
        raise HTTPException(400, "Selecione ao menos 1 distribuidora e 1 ano.")

    # se quiser mapear UI->DB, mantenha aqui
    dists_db = [UI_TO_DB.get(d, d) for d in dists_ui]

    rows = (await db.execute(q_catalog, {"dists": tuple(dists_upper), "anos": tuple(anos)})).mappings().all()
    if not rows:
        return {"enfileirados": 0, "aviso": "Nenhuma URL encontrada para os filtros."}

    # não precisa de create_task dentro do background
    background.add_task(download_many, rows, DOWNLOAD_BASE)

    return {
        "enfileirados": len(rows),
        "destino": str(DOWNLOAD_BASE),
        "mensagem": "Importação iniciada. Os arquivos serão gravados por distribuidora/ano."
    }


@router.get("/import-status")
async def import_status(db: AsyncSession = Depends(get_session), limit: int = Query(200, ge=1, le=500)):
    q = text(f"""
      SELECT import_id, distribuidora_nome, ano, camada, status, observacoes, erro,
             COALESCE(data_fim, data_inicio, now()) AS data_ref
        FROM {DB_SCHEMA}.import_status
       ORDER BY COALESCE(data_fim, data_inicio, now()) DESC
       LIMIT :limit
    """)
    rs = await db.execute(q, {"limit": limit})
    out = []
    for r in rs.fetchall():
        m = r._mapping
        obs_parsed = _parse_obs(m["observacoes"])
        out.append({
            "id": str(m["import_id"]),                    
            "distribuidora": m["distribuidora_nome"],
            "ano": int(m["ano"]),
            "camada": m["camada"],
            "status": map_status_ui(m["status"], obs_parsed),
            "data_execucao": m["data_ref"].isoformat(),
            "observacoes": obs_parsed,                     
            "erro": m["erro"],
        })
    return out


@router.get("/leads/status-count")
async def leads_status_count():
    return {"total": 0, "em_aberto": 0, "em_progresso": 0, "concluido": 0}

@router.post("/import/cancel/{import_id}")
async def import_cancel(import_id: str, db: AsyncSession = Depends(get_session)):
    # tenta cancelar job em execução
    q_run = text(f"""
      UPDATE {DB_SCHEMA}.import_status
         SET cancel_requested = TRUE,
             observacoes = jsonb_build_object('ts', (EXTRACT(EPOCH FROM now())*1000)::bigint, 'phase','canceled','message','Cancelado pelo usuário')
       WHERE import_id = :id AND status = 'running'
       RETURNING import_id
    """)
    r1 = await db.execute(q_run, {"id": import_id})
    if r1.first():
        await db.commit()
        return {"ok": True, "mode": "running-flagged"}

    # se ainda estava pending, cancela de pronto
    q_pend = text(f"""
      UPDATE {DB_SCHEMA}.import_status
         SET status = 'canceled',
             cancel_requested = FALSE,
             data_fim = now(),
             observacoes = jsonb_build_object('ts', (EXTRACT(EPOCH FROM now())*1000)::bigint, 'phase','canceled','message','Cancelado pelo usuário')
       WHERE import_id = :id AND status = 'pending'
       RETURNING import_id
    """)
    r2 = await db.execute(q_pend, {"id": import_id})
    if r2.first():
        await db.commit()
        return {"ok": True, "mode": "pending-closed"}

    await db.rollback()
    raise HTTPException(404, "Not Found")

@router.post("/import/restart/{import_id}")
async def import_restart(import_id: str, db: AsyncSession = Depends(get_session)):
    q = text(f"""
      UPDATE {DB_SCHEMA}.import_status
         SET status='pending',
             cancel_requested = FALSE,
             linhas_processadas = 0,
             data_inicio = NULL,
             data_fim = NULL,
             erro = NULL,
             observacoes = jsonb_build_object('ts', (EXTRACT(EPOCH FROM now())*1000)::bigint, 'phase','restarted')
       WHERE import_id = :id
       RETURNING import_id
    """)
    r = await db.execute(q, {"id": import_id})

    if not r.first():
      await db.rollback()
      raise HTTPException(404, "Not Found")
    await db.commit()
    return {"ok": True}


from fastapi import Query, HTTPException

@router.get("/import/layers")
async def import_layers(
    distribuidora: str = Query(...),
    ano: int = Query(..., ge=1900, le=2100),
    deep: bool = Query(False, description="Se true e Fiona disponível, extrai o ZIP e lista as layers do .gdb"),
):
    base = DOWNLOAD_BASE / safe_name(distribuidora) / str(ano)
    if not base.exists():
        raise HTTPException(status_code=404, detail=f"Pasta não encontrada: {base}")

    files = sorted([p for p in base.iterdir() if p.is_file()], key=lambda x: x.name.lower())
    items = [_inspect_any_file(p, deep=deep) for p in files]

    # também lista .gdb/.shp diretamente na pasta (se existirem)
    gdb_dirs = sorted([p.name for p in base.glob("*.gdb") if p.is_dir()])
    shp_layers = sorted({p.stem for p in base.glob("*.shp")})

    return {
        "root": str(base),
        "fiona_available": _HAS_FIONA,
        "deep_used": bool(deep and _HAS_FIONA),
        "files": items,
        "gdb_dirs": gdb_dirs,
        "shp_layers": shp_layers,
        "hint": "Use ?deep=true para listar gdb_layers; exige Fiona instalada.",
    }
