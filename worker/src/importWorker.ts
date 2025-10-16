// worker/src/importWorker.ts
import { fileURLToPath } from 'node:url';
import * as path from 'node:path';
import * as fs from 'node:fs';
import dotenv from 'dotenv';

// Em Node >= 18 já existe AbortController global; se estiver em Node 16, instale o pacote abaixo:
// import { AbortController } from 'abort-controller';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
dotenv.config({ path: path.resolve(__dirname, '..', '.env'), override: true });

import { Pool } from 'pg';
import fetch from 'node-fetch';
import * as unzipper from 'unzipper';
import { pipeline } from 'stream/promises';
import { Readable, Transform } from 'stream';

/* =========================
 * Helpers de ambiente/SSL
 * ========================= */
function wantsSSL(dbUrl?: string) {
  if (!dbUrl) return false;
  try {
    const u = new URL(dbUrl);
    const sm = (u.searchParams.get('sslmode') || '').toLowerCase();
    return ['require', 'verify-ca', 'verify-full'].includes(sm)
      || /azure|neon|supabase|amazonaws|render\.com|cockroach/i.test(u.host);
  } catch {
    return false;
  }
}

const DBURL  = process.env.DATABASE_URL!;
const SCHEMA = process.env.DB_SCHEMA || 'intel_lead';
const useSSL = wantsSSL(DBURL);

const pool = new Pool({
  connectionString: DBURL,
  ssl: useSSL ? { rejectUnauthorized: false } : undefined,
  connectionTimeoutMillis: 8000,
});

const BASE_DIR = process.env.DOWNLOAD_DIR ?? path.resolve(process.cwd(), 'data', 'downloads');
const BATCH = Number(process.env.WORKER_BATCH ?? 2);

console.log(
  '[worker] DB host:',
  (() => { try { return new URL(DBURL).host } catch { return '(URL inválida)' } })(),
  'ssl=', useSSL
);
console.log('[worker] BASE_DIR:', BASE_DIR);

/* =========================
 * Constantes de status
 * ========================= */
const STATUS = {
  pending:   'pending',
  running:   'running',
  completed: 'completed',
  failed:    'failed',
  canceled:  'canceled', // <- adicionamos esse estado lógico
} as const;

/* =========================
 * Funções utilitárias
 * ========================= */
async function isCanceled(importId: string) {
  const { rows } = await pool.query(
    `SELECT cancel_requested FROM ${SCHEMA}.import_status WHERE import_id=$1`,
    [importId]
  );
  return !!rows[0]?.cancel_requested;
}

async function getUrl(distrib: string, ano: number) {
  const { rows } = await pool.query(
    `SELECT url FROM ${SCHEMA}.dataset_url_normalized
      WHERE distribuidora = $1 AND ano = $2
      LIMIT 1`,
    [distrib, ano]
  );
  return rows[0]?.url ?? null;
}

function ensureJson(value: any) {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return { ts: Date.now(), ...value };
  }
  return value;
}

function setObs(importId: string, payload: any) {
  const obs = ensureJson(payload);
  return pool.query(
    `UPDATE ${SCHEMA}.import_status SET observacoes=$2 WHERE import_id=$1`,
    [importId, obs]
  );
}

async function markCanceled(importId: string, message = 'Cancelado pelo usuário') {
  await pool.query(
    `UPDATE ${SCHEMA}.import_status
        SET status=$2, erro=$3, data_fim=now(), cancel_requested=false,
            observacoes=$4
      WHERE import_id=$1`,
    [importId, STATUS.canceled, message, { ts: Date.now(), phase: 'canceled', message }]
  );
}

async function markFailed(importId: string, message: string) {
  await pool.query(
    `UPDATE ${SCHEMA}.import_status
        SET status=$2, erro=$3, data_fim=now(),
            observacoes=$4
      WHERE import_id=$1`,
    [importId, STATUS.failed, message, { ts: Date.now(), phase: 'error', message }]
  );
}

async function markCompleted(importId: string, camada: string | null, unzipDest: string) {
  await pool.query(
    `UPDATE ${SCHEMA}.import_status
        SET status=$2,
            camada = COALESCE($3, camada),
            data_fim = now(),
            observacoes = $4
      WHERE import_id = $1`,
    [importId, STATUS.completed, camada, { ts: Date.now(), phase: 'completed', path: unzipDest }]
  );
}

async function cleanupFiles(zipPath: string, unzipDest: string) {
  await fs.promises.rm(zipPath, { force: true }).catch(() => {});
  await fs.promises.rm(unzipDest, { recursive: true, force: true }).catch(() => {});
}

/* =========================
 * Processamento de um job
 * ========================= */
async function processJob(j: { import_id: string; distribuidora_nome: string; ano: number; }) {
  // cancelado antes de começar?
  if (await isCanceled(j.import_id)) {
    return markCanceled(j.import_id);
  }

  const url = await getUrl(j.distribuidora_nome, j.ano);
  if (!url) {
    return markFailed(j.import_id, 'URL não encontrada na view');
  }

  // Pastas/arquivos
  const destDir   = path.join(BASE_DIR, j.distribuidora_nome, String(j.ano));
  const zipPath   = path.join(destDir, `dataset_${j.ano}.zip`);
  const unzipDest = path.join(destDir, 'unzipped');

  await fs.promises.mkdir(destDir, { recursive: true });
  // reinício limpo: remove restos de downloads anteriores
  await cleanupFiles(zipPath, unzipDest);

  try {
    console.log(`[worker] baixando ${j.distribuidora_nome} ${j.ano} -> ${zipPath}`);

    // início do download
    await setObs(j.import_id, { phase: 'downloading', downloaded: 0, total: null, pct: null, speed: null });

    // Controlador para permitir abortar fetch
    const controller = new AbortController();
    const res = await fetch(url, { signal: controller.signal as any });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    if (!res.body) throw new Error('Resposta sem corpo');

    const total = Number(res.headers.get('content-length') || '') || null;
    let downloaded = 0;
    let lastFlush = Date.now();
    let lastBytes = 0;
    let lastPct: number | null = null;
    let lastCancelCheck = 0;

    const ws = fs.createWriteStream(zipPath);

    const nodeReadable =
      typeof (res.body as any).pipe === 'function'
        ? (res.body as unknown as NodeJS.ReadableStream)
        : Readable.fromWeb(res.body as any);

    const progressTap = new Transform({
      async transform(chunk, _enc, cb) {
        downloaded += (chunk as Buffer).length;
        const now = Date.now();
        const dt = now - lastFlush;
        const pct = total ? Math.max(0, Math.min(100, Math.round((downloaded / total) * 100))) : null;

        // atualiza progresso ~750ms ou quando % sobe 1 ponto
        const pctStep = pct !== null && lastPct !== null && pct >= lastPct + 1;
        if (dt >= 750 || pctStep) {
          const speed = (downloaded - lastBytes) / (dt / 1000);
          lastFlush = now;
          lastBytes = downloaded;
          lastPct = pct ?? lastPct;
          setObs(j.import_id, { phase: 'downloading', downloaded, total, pct, speed }).catch(() => {});
        }

        // checagem de cancelamento ~1s
        if (now - lastCancelCheck > 1000) {
          lastCancelCheck = now;
          try {
            if (await isCanceled(j.import_id)) {
              controller.abort(); // aborta fetch
              return cb(new Error('USER_CANCEL'));
            }
          } catch { /* ignore */ }
        }

        cb(null, chunk);
      }
    });

    // grava o zip com o progress tap
    await pipeline(nodeReadable, progressTap, ws);

    // flush final
    {
      const pct = total ? Math.max(0, Math.min(100, Math.round((downloaded / total) * 100))) : null;
      await setObs(j.import_id, { phase: 'downloading', downloaded, total, pct, speed: null });
    }

    // se cancelou exatamente ao terminar o download, respeita
    if (await isCanceled(j.import_id)) {
      await cleanupFiles(zipPath, unzipDest);
      return markCanceled(j.import_id);
    }

    // === EXTRAÇÃO ===
    await setObs(j.import_id, { phase: 'extracting' });
    await fs.promises.mkdir(unzipDest, { recursive: true });

    // checa cancelamento novamente antes de extrair
    if (await isCanceled(j.import_id)) {
      await cleanupFiles(zipPath, unzipDest);
      return markCanceled(j.import_id);
    }

    await fs.createReadStream(zipPath).pipe(unzipper.Extract({ path: unzipDest })).promise();

    // tenta inferir camada
    let camada: string | null = null;
    try {
      const files = await fs.promises.readdir(unzipDest);
      const hit = files.find(f => /UCMT|UCBT|UCAT|PONNOT/i.test(f));
      if (hit) {
        const m = hit.match(/(UCMT|UCBT|UCAT|PONNOT)/i);
        camada = m ? m[1]!.toUpperCase() : null;
      }
    } catch { /* ignore */ }

    await markCompleted(j.import_id, camada, unzipDest);
  } catch (e: any) {
    const msg = String(e?.message ?? e);
    const canceled = /USER_CANCEL|aborted|AbortError/i.test(msg);
    await cleanupFiles(zipPath, canceled ? unzipDest : ''); // no cancel, remova parciais
    if (canceled) {
      await markCanceled(j.import_id);
    } else {
      await markFailed(j.import_id, msg);
    }
  }
}

/* =========================
 * Loop principal
 * ========================= */
async function loop() {
  while (true) {
    const client = await pool.connect();
    try {
      await client.query('BEGIN');

      // pega jobs pendentes com lock
      const { rows: jobs } = await client.query(
        `
        SELECT import_id, distribuidora_nome, ano
          FROM ${SCHEMA}.import_status
         WHERE status = $1
         ORDER BY ano, distribuidora_nome
         FOR UPDATE SKIP LOCKED
         LIMIT $2
        `,
        [STATUS.pending, BATCH]
      );

      if (!jobs.length) {
        await client.query('COMMIT');
        // silêncio por 3s
        await new Promise(r => setTimeout(r, 3000));
        continue;
      }

      // marca como running + fase inicial
      for (const j of jobs) {
        await client.query(
          `UPDATE ${SCHEMA}.import_status
              SET status=$2, data_inicio=now(), erro=NULL, cancel_requested=false
            WHERE import_id=$1`,
          [j.import_id, STATUS.running]
        );
        await setObs(j.import_id, { phase: 'starting' });
      }
      await client.query('COMMIT'); // solta os locks

      // processa fora da transação
      for (const j of jobs) {
        await processJob(j);
      }
    } catch (e) {
      await pool.query('ROLLBACK').catch(() => {});
      console.error('[worker] erro no loop:', e);
    } finally {
      client.release();
    }
  }
}

loop().catch(console.error);
