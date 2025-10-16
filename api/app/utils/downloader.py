import aiohttp
import aiofiles
from pathlib import Path
import re
import asyncio
import os
import json
from typing import Awaitable, Callable, Optional, Union, Any, Dict, List
import ast


ProgressCB = Optional[Callable[[dict], Awaitable[None]]]
SAFE_CHUNK = 1024 * 128
def safe_name(s: str) -> str:
    s = (s or "").strip().replace("/", "-")
    s = re.sub(r"[^\w\-\.\s]", "_", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s)
    return s[:150]

async def download_one(session: aiohttp.ClientSession, url: str, dst: Path) -> dict:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return {"url": url, "ok": False, "status": resp.status}
            async with aiofiles.open(tmp, "wb") as f:
                async for chunk in resp.content.iter_chunked(SAFE_CHUNK):
                    await f.write(chunk)
        tmp.replace(dst)
        return {"url": url, "ok": True, "status": 200, "path": str(dst)}
    except Exception as e:
        try:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return {"url": url, "ok": False, "error": str(e)}


async def download_many(rows: list[dict], base_dir: Path, progress_cb=None) -> dict:
    results = []
    timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_connect=30, sock_read=None)
    connector = aiohttp.TCPConnector(limit=6)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        tasks = []
        for r in rows:
            dist  = safe_name(r.get("distribuidora") or "desconhecida")
            ano   = str(r.get("ano") or "s_ano")
            title = safe_name(r.get("title") or "")
            ext   = os.path.splitext((r.get("url") or "").split("?")[0].split("#")[0])[1] or ""

            if title:
                fname = safe_name(f"{title}{ext}")
            else:
                leaf = (r.get("url") or "").rstrip("/").split("/")[-1] or "arquivo"
                fname = safe_name(leaf)

            dst = base_dir / dist / ano / fname
            tasks.append(download_one(session, r["url"], dst))

        done = fail = 0
        total = len(tasks)

        for coro in asyncio.as_completed(tasks):
            res = await coro
            results.append(res)
            if res.get("ok"): done += 1
            else: fail += 1

            if progress_cb:
                pct = int(round(100 * done / total)) if total else 0
                await progress_cb({"done": done, "fail": fail, "total": total, "pct": pct})

    ok = sum(1 for x in results if x.get("ok"))
    fail = len(results) - ok
    if progress_cb:
        await progress_cb({"done": ok, "fail": fail, "total": len(results), "pct": 100, "finished": True})
    return {"baixados": ok, "falhas": fail, "detalhes": results}


def map_status_ui(status: str, observacoes: dict | None = None) -> str:
    status = (status or "").lower()

    mapping = {
        "done": "concluido",
        "completed": "concluido",
        "error": "erro",
        "downloading": "baixando",
        "extracting": "extraindo",
        "importing": "importando",
        "pending": "pendente",
    }

    # Caso haja lógica extra baseada em observacoes
    if observacoes and observacoes.get("fase") == "download":
        return "baixando"
    if observacoes and observacoes.get("fase") == "extract":
        return "extraindo"

    return mapping.get(status, "pendente")


def _parse_obs(obs_raw: Union[str, bytes, bytearray, Dict[str, Any], List[Any], None]):
    # Sem observações -> objeto vazio
    if obs_raw is None or obs_raw == "":
        return {}

    # Se já vier como dict/list (JSON já parseado pelo driver), devolve do jeito que está
    if isinstance(obs_raw, (dict, list)):
        return obs_raw

    # Se vier como bytes/bytearray, decodifica
    if isinstance(obs_raw, (bytes, bytearray)):
        try:
            obs_raw = obs_raw.decode("utf-8", errors="ignore")
        except Exception:
            # fallback: retorna representação crua
            return {"raw": str(obs_raw)}

    # Se for string, tenta parsear como JSON
    if isinstance(obs_raw, str):
        s = obs_raw.strip()
        # caminho feliz
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            # tentativa de "consertar" strings com aspas simples/trailing commas etc.
            try:
                val = ast.literal_eval(s)
                if isinstance(val, (dict, list)):
                    return val
                # se for outro tipo (ex.: tuple), embrulha
                return {"raw": val}
            except Exception:
                # última linha de defesa: devolve bruto para não quebrar a API
                return {"raw": s}

    # Tipo inesperado: devolve representação crua
    return {"raw": str(obs_raw)}