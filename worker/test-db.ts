// worker/test-db.ts
import * as path from 'node:path';
import * as url from 'node:url';
import dotenv from 'dotenv';
const __dirname = path.dirname(url.fileURLToPath(import.meta.url));
dotenv.config({ path: path.resolve(__dirname, '.env') });

import { Client } from 'pg';

// decide SSL baseado na URL (?sslmode=require) ou PGSSL=true
function wantsSSL(dbUrl?: string) {
  if (!dbUrl) return false;
  try {
    const u = new URL(dbUrl);
    const sm = (u.searchParams.get('sslmode') || '').toLowerCase();
    return ['require','verify-ca','verify-full'].includes(sm)
        || /azure|neon|supabase|amazonaws|render\.com|cockroach/i.test(u.host);
  } catch { return false; }
}

const DBURL = process.env.DATABASE_URL;
const useSSL = wantsSSL(DBURL) || (process.env.PGSSL ?? '').toLowerCase() === 'true';

function mask(u?: string) {
  return u ? u.replace(/:(.*?)@/, ':***@') : u;
}

async function main() {
  console.log('DATABASE_URL:', mask(DBURL));
  console.log('SSL:', useSSL);

  if (!DBURL) {
    console.error('❌ DATABASE_URL não definida (verifique worker/.env)');
    process.exit(1);
  }

  const client = new Client({
    connectionString: DBURL,
    ssl: useSSL ? { rejectUnauthorized: false } : undefined,
    connectionTimeoutMillis: 8000,
  });

  try {
    console.log('Conectando...');
    await client.connect();
    console.log('✅ Conectado!');
    const { rows } = await client.query(`
      select current_user,
             current_database() as database,
             current_schema()  as schema,
             now()              as agora
    `);
    console.log(rows[0]);

    // teste extra: contar registros da view/tabela (ajuste schema se precisar)
    const SCHEMA = process.env.DB_SCHEMA || 'intel_lead';
    const { rows: r2 } = await client.query(`select count(*)::int as n from ${SCHEMA}.dataset_url_normalized`);
    console.log(`dataset_url_normalized: ${r2[0].n} registros`);
  } catch (err) {
    console.error('❌ Erro ao conectar/consultar:', err);
  } finally {
    await client.end().catch(()=>{});
    console.log('Fechou conexão.');
  }
}

main().catch(e => console.error('❌ Erro não tratado:', e));
