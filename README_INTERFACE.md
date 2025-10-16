# 🔎 Buscador de CEP no GDB

Interface web para visualizar e analisar dados de unidades consumidoras de energia elétrica.

## 🚀 Como Usar

### 1. Abra o arquivo HTML
- Dê duplo-clique em `index.html`
- Ou arraste para o navegador

### 2. Carregue um arquivo GeoJSON
- Clique em "Escolher arquivo"
- Selecione um arquivo `.geojson` (ex: `cep_01310200_matches.geojson`)
- O arquivo será carregado automaticamente

### 3. Navegue pelos endereços
- Clique em qualquer endereço da lista para ver detalhes

## 📊 Funcionalidades

### Lista de Endereços
- Endereços agrupados por localização
- Indicadores **DIC** (Duração de Interrupção em horas)
- Indicadores **FIC** (Frequência de Interrupção em vezes)
- Valores em amarelo indicam problemas

### Modal Detalhado
Ao clicar em um endereço você vê:

**📋 Identificação:**
- Endereço, CEP, Ponto de Conexão
- Status (ATIVO/DESLIGADO)
- Data de conexão

**🏢 Classificação:**
- Tipo de cliente (Residencial, Comercial, Industrial)
- CNAE, Grupo tarifário

**⚡ Dados Técnicos:**
- Carga instalada, Tensão, Fases
- Transformador, Circuito, Subestação

**📈 Gráficos:**
- Consumo mensal de energia (ENE)
- DIC e FIC por mês

## 🔧 Busca por CEP (Opcional)

Se a API estiver rodando:
```bash
uvicorn app:app --reload
```

Você pode buscar diretamente por CEP digitando no campo de busca.

## 💾 Baixar Dados

Use o botão "Baixar JSON" para exportar os dados carregados.

