# app.py
import streamlit as st
import pandas as pd
import numpy as np
import io
import requests
import re
import html
import gspread
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import Request
from datetime import datetime
from zoneinfo import ZoneInfo

# Streamlit Cloud roda em UTC; os horários de auditoria precisam sair em Brasília
FUSO_BR = ZoneInfo("America/Sao_Paulo")

def agora_br() -> datetime:
    return datetime.now(FUSO_BR)

# ═══════════════════════════════════════════════════════════
# REGRAS DE NEGÓCIO
# ═══════════════════════════════════════════════════════════
SITUACOES_VALIDAS  = {'não informado', 'uso autorizado', 'doc não gerado'}
TERMOS_EXCLUSAO    = r'comodato|retorno|devolu[çc][aã]o'
COLUNAS_EXCLUSAO   = ['Observação', 'Natureza Operação', 'Tipo Documento SAP']
MAX_DOWNLOAD_MB    = 50
TIMEOUT_S          = 120
LIXO               = {'nan', 'none', '', 'nat', '<na>', 'null'}
ABA_CONTROLE       = 'Controle'
COLS_CONTROLE      = ['NF', 'CNPJ', 'Tratado', 'Observacao_Controle', 'Responsavel', 'Atualizado_em']

# ── Colunas usadas (busca tolerante: ignora acento/maiúscula/apóstrofo) ──
# Cruzamento principal: CHAVE DE ACESSO (SAP 'Chave de Acesso' x Qive 'Chave de acesso')
COL_NF_SAP_CAND     = ['Nº NF', 'N° NF', 'No NF', 'Numero NF', 'Número NF', 'NF']
COL_NF_QIVE_CAND    = ['Número', 'Numero', 'Nº NF', 'N° NF', 'Nº Nota', 'NF']
COL_NF_NFS_CAND     = ['Nr. NF', 'Nr NF', 'Nr.NF', 'Nº NF', 'N° NF', 'Numero NF', 'Número NF',
                       'Número', 'Numero']

COL_CNPJ_SAP_CAND   = ['CNPJ / CPF Emitente', 'CNPJ/CPF Emitente', 'CNPJ / CPF', 'CNPJ/CPF',
                       'CNPJ Emitente', 'CNPJ', 'CPF/CNPJ Emitente']
COL_CNPJ_QIVE_CAND  = ['CNPJ Emitente', 'CNPJ do Emitente', 'CNPJ']
COL_CPF_QIVE_CAND   = ['CPF Emitente', 'CPF do Emitente', 'CPF']
COL_CNPJ_NFS_CAND   = ['CNPJ ou CPF', 'CNPJ/CPF', 'CNPJ / CPF', 'CNPJ', 'CPF/CNPJ', 'CNPJ Emitente']

COL_DESC_CAND       = ['[Item] Descrição', '[Item] Descricao',
                       'Descrição do item/serviço', 'Descrição do item/servico',
                       'Descrição do Item/Serviço', 'Descrição item', 'Descrição do item',
                       'Descrição', 'Descricao', 'Item/Serviço', 'Item/Servico', 'Item']

COL_CC_CAND         = ['Centro de Custos', 'Centro de Custo', 'Centro Custo', 'Centro de custos',
                       'CC', 'Cost Center', 'Centro_de_Custos']

COL_CFOP_CAND       = ['CFOP', 'C.F.O.P', 'Cfop', 'CFOP SAP']

# CFOP de retorno / devolução / conserto → fora do escopo de manifestação.
# Derivado da base real (campo Natureza Operação = retorno/devolução/conserto).
CFOP_RETORNO_DEVOLUCAO = {
    '1909', '2202', '2909',
    '5202', '5902', '5909', '5910', '5915', '5916',
    '6202', '6556', '6909', '6910', '6915', '6916', '6918',
}

# NFs: nome do fornecedor e colunas de valor (para casar por nome + valor)
COL_NOME_NFS_CAND   = ['Nome PJ Emitente', 'Nome do cliente/fornecedor', 'Nome cliente/fornecedor',
                       'Nome Fornecedor', 'Fornecedor', 'Nome/Razão Social', 'Razão Social', 'Nome']
COL_VAL_NFS_CAND    = ['Total da linha', 'Total documento', 'Valor', 'Total']
# SAP: valor da nota
COL_VAL_SAP_CAND    = ['Valor Nota Fiscal', 'Valor da Nota', 'Valor Total', 'Valor']

ABA_NFS_CAND        = ["NFs", "NF's", "NF´s", "NFES", "NFE", "NF-e", "NF", "NOTAS", "DETALHES"]

COLUNAS_EXIBIR = [
    'Nº NF',
    'CNPJ Emitente',
    'Razão Social',
    'Valor Nota Fiscal',
    'Situação Manifestação',
    'Centro de Custo',
    'Na NFs',
    'Chave de Acesso',
]

st.set_page_config(page_title="Manifestações NF-e", layout="wide", initial_sidebar_state="collapsed")

# ═══════════════════════════════════════════════════════════
# IDENTIDADE VISUAL — Design System Cobli (estilo Vega, modo Light)
# Cores base também em .streamlit/config.toml (tabela e widgets nativos).
# ═══════════════════════════════════════════════════════════
COBLI_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600&family=Geist+Mono:wght@400;500&display=swap');
:root {
  --background:#FFFFFF; --foreground:#020231; --card:#FFFFFF; --primary:#001BB8;
  --primary-foreground:#FAFAFA; --secondary:#DCEBFE; --secondary-foreground:#07074F;
  --accent:#F4F4F5; --muted:#F4F4F5; --muted-foreground:#71717A; --border:#E4E4E7;
  --ring:#A1A1AA; --destructive:#DC2626; --success:#0D9488; --warning:#EA580C;
  --highlight:#00C2A8; --outline:#001BB880;
}
html, body, .stApp, .stMarkdown, p, label, input, textarea, button, h1, h2, h3, h4,
[data-baseweb="tab"], [data-baseweb="select"], [data-testid="stCaptionContainer"] {
  font-family:'Geist', system-ui, sans-serif !important;
}
.stApp { background:var(--background); color:var(--foreground); }
[data-testid="stHeader"] { background:transparent; }
.block-container, [data-testid="stAppViewBlockContainer"] { padding-top:2.2rem; padding-bottom:3rem; max-width:1440px; }

/* PageHeader */
.cb-header { display:flex; align-items:flex-end; justify-content:space-between; gap:16px; }
.cb-brand { font-size:12px; font-weight:600; letter-spacing:.08em; text-transform:uppercase; color:var(--primary); margin-bottom:4px; }
.cb-title { font-size:24px; line-height:32px; font-weight:600; color:var(--foreground); margin:0; }
.cb-sub { font-size:14px; color:var(--muted-foreground); margin-top:4px; }

/* KPI Cards */
.cb-kpis { display:grid; grid-template-columns:repeat(5, minmax(0,1fr)); gap:12px; margin:20px 0 8px; }
@media (max-width:1024px){ .cb-kpis { grid-template-columns:repeat(2, minmax(0,1fr)); } }
.cb-kpi { background:var(--card); border:1px solid var(--border); border-radius:14px; padding:18px 20px;
          box-shadow:0 1px 2px rgba(2,2,49,.05); position:relative; overflow:hidden; }
.cb-kpi::before { content:""; position:absolute; left:0; top:0; bottom:0; width:3px; background:var(--kpi-cor, var(--border)); }
.cb-kpi-label { font-size:12px; font-weight:500; letter-spacing:.04em; text-transform:uppercase; color:var(--muted-foreground);
                white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.cb-kpi-valor { font-size:28px; line-height:36px; font-weight:600; color:var(--foreground); margin-top:6px;
                font-variant-numeric:tabular-nums; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.cb-kpi-nota { font-size:12px; color:var(--muted-foreground); margin-top:2px; }
.cb-kpi-nota b { color:var(--kpi-cor, var(--foreground)); font-weight:600; }

/* Tabs (Vega) */
[data-baseweb="tab-list"] { background:var(--muted); border-radius:10px; padding:4px; gap:4px; width:fit-content; max-width:100%; overflow-x:auto; }
[data-baseweb="tab"] { border-radius:8px !important; padding:6px 12px !important; height:auto !important; background:transparent; }
[data-baseweb="tab"] p { font-size:14px !important; font-weight:500; color:var(--muted-foreground); }
[data-baseweb="tab"][aria-selected="true"] { background:var(--background); box-shadow:0 1px 2px rgba(2,2,49,.08); }
[data-baseweb="tab"][aria-selected="true"] p { color:var(--foreground); }
[data-baseweb="tab-highlight"], [data-baseweb="tab-border"] { display:none; }
[data-testid="stTabs"] [data-baseweb="tab-panel"] { padding-top:16px; }

/* Buttons / Inputs (Vega: radius 8, altura 36) */
.stButton > button, .stDownloadButton > button { border-radius:8px; min-height:36px; padding:6px 16px; font-weight:500;
  font-size:14px; white-space:nowrap; border:1px solid var(--border); }
.stButton > button[kind="primary"] { background:var(--primary); border-color:var(--primary); color:var(--primary-foreground); }
.stButton > button[kind="primary"]:hover { background:#0248E9; border-color:#0248E9; }
.stButton > button:disabled, .stButton > button[kind="primary"]:disabled { background:var(--muted); border-color:var(--border); color:var(--ring); }
.stButton > button:focus-visible, .stDownloadButton > button:focus-visible { box-shadow:0 0 0 3px var(--outline); }
[data-baseweb="input"], [data-baseweb="select"] > div { border-radius:8px !important; border-color:var(--border) !important; }
[data-testid="stCheckbox"] label p { white-space:nowrap; }
[data-testid="stHorizontalBlock"] { align-items:center; }

/* Tabela */
[data-testid="stDataFrame"] { border:1px solid var(--border); border-radius:10px; overflow:hidden; }

/* Painel de detalhes */
.cb-card { background:var(--card); border:1px solid var(--border); border-radius:14px; padding:20px 24px; margin-top:8px; }
.cb-card-titulo { font-size:16px; font-weight:600; color:var(--foreground); margin-bottom:14px; display:flex; gap:8px; align-items:center; }
.cb-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(200px,1fr)); gap:14px 24px; }
.cb-campo-label { font-size:12px; font-weight:500; color:var(--muted-foreground); text-transform:uppercase; letter-spacing:.04em; }
.cb-campo-valor { font-size:14px; color:var(--foreground); margin-top:2px; word-break:break-word; }
.cb-mono { font-family:'Geist Mono', ui-monospace, monospace !important; font-size:13px; }
.cb-desc { margin-top:16px; padding-top:14px; border-top:1px solid var(--border); }
.cb-badge { display:inline-block; border-radius:8px; padding:2px 10px; font-size:12px; font-weight:500; }
.cb-badge-ok { background:#F0FDFA; color:var(--success); }
.cb-badge-pend { background:#FFF7ED; color:var(--warning); }
.cb-badge-neutro { background:var(--muted); color:var(--muted-foreground); }
</style>
"""
st.markdown(COBLI_CSS, unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════
# UTILITÁRIOS
# ═══════════════════════════════════════════════════════════
def norm_nf(s: pd.Series) -> pd.Series:
    return s.astype(str).str.strip().str.replace(r'\.0$', '', regex=True)

def norm_nf_val(v) -> str:
    return re.sub(r'\.0$', '', str(v).strip())

def norm_cnpj(s: pd.Series) -> pd.Series:
    return (s.astype(str).str.replace(r'\.0$', '', regex=True)
             .str.replace(r'\D', '', regex=True).str.strip())

def norm_cnpj_val(v) -> str:
    return re.sub(r'\D', '', re.sub(r'\.0$', '', str(v).strip()))

def norm_valor(v):
    """Converte valor monetário em float (2 casas). Trata R$, milhar e vírgula decimal."""
    t = str(v).strip()
    if not t or t.lower() in LIXO:
        return None
    t = re.sub(r'[^\d,.-]', '', t)
    if ',' in t and '.' in t:      # ex.: 1.234,56 -> ponto é milhar
        t = t.replace('.', '').replace(',', '.')
    elif ',' in t:                 # ex.: 1234,56
        t = t.replace(',', '.')
    try:
        return round(float(t), 2)
    except ValueError:
        return None

def fmt_brl(v) -> str:
    """22670.4 -> 'R$ 22.670,40' (vazio se não for número)."""
    x = v if isinstance(v, (int, float)) and not pd.isna(v) else norm_valor(v)
    if x is None:
        return ''
    return 'R$ ' + f"{x:,.2f}".replace(',', '_').replace('.', ',').replace('_', '.')

def fmt_doc(v) -> str:
    """CNPJ/CPF só com dígitos -> com máscara. Outros formatos voltam como vieram."""
    d = norm_cnpj_val(v)
    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    if len(d) == 11:
        return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"
    return str(v)

def norm_chave(s: pd.Series) -> pd.Series:
    return s.astype(str).str.replace(r'\D', '', regex=True).str.strip()

def norm_chave_val(v) -> str:
    return re.sub(r'\D', '', str(v).strip())

def normalizar(s: pd.Series) -> pd.Series:
    return s.astype(str).str.strip().str.upper()

def _limpa_nome(v: str) -> str:
    """Normaliza razão social para comparação: sem acento, sem pontuação,
    sem sufixos societários e sem o CNPJ que às vezes vem colado no nome."""
    import unicodedata
    t = str(v).strip().lower()
    t = ''.join(c for c in unicodedata.normalize('NFKD', t) if not unicodedata.combining(c))
    t = re.sub(r'\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}', ' ', t)  # remove CNPJ colado
    t = re.sub(r'\d{3}\.?\d{3}\.?\d{3}-?\d{2}', ' ', t)          # remove CPF colado
    t = re.sub(r'\b(ltda|me|epp|eireli|s\.?a|sa|cia|comercio|com|ind|industria)\b', ' ', t)
    t = re.sub(r'[^a-z0-9 ]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t

def _nomes_parecidos(a: str, b: str) -> bool:
    """True se os nomes coincidem o suficiente (ignora vírgula, LTDA, acento…)."""
    na, nb = _limpa_nome(a), _limpa_nome(b)
    if not na or not nb:
        return True  # se um lado está vazio, não acusa divergência de nome
    if na == nb or na in nb or nb in na:
        return True
    # sobreposição de palavras significativas (>= 60%)
    sa, sb = set(na.split()), set(nb.split())
    if not sa or not sb:
        return True
    inter = len(sa & sb)
    return inter / min(len(sa), len(sb)) >= 0.6

# Mapeia os vocabulários de manifestação (SAP x Qive) para um estado comum
def _estado_manif(valor: str) -> str:
    v = re.sub(r'\s+', ' ', str(valor).strip().lower())
    if v in LIXO or v == '':
        return 'vazio'
    if 'sem manifest' in v or 'não informado' in v or 'nao informado' in v or 'desconhec' in v:
        return 'sem'
    if 'confirm' in v or 'ciente' in v:            # SAP 'Confirmação da Operação' / Qive 'Ciente'/'Confirmada'
        return 'manifestada'
    if 'não realizada' in v or 'nao realizada' in v or 'operação não' in v or 'operacao nao' in v:
        return 'nao_realizada'
    return 'outro'

def _conferir_manifestacao(sit_sap: str, manif_qive: str) -> str:
    a = _estado_manif(sit_sap)
    b = _estado_manif(manif_qive)
    if b == 'vazio':
        return '— (sem manifestação na Qive)'
    if a == b:
        return '✅ Confere'
    if a in ('sem', 'vazio') and b == 'manifestada':
        return '⚠️ Qive manifestou, SAP não'
    if a == 'manifestada' and b in ('sem', 'vazio'):
        return '⚠️ SAP manifestou, Qive não'
    return '⚠️ Divergente'

def limpar_display(df: pd.DataFrame) -> pd.DataFrame:
    return df.astype(object).replace(
        {np.nan: '', pd.NaT: '', 'None': '', 'nan': '', '<NA>': '', 'NaT': ''}
    )

def btn_exportar(df: pd.DataFrame, nome: str):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as w:
        df.to_excel(w, index=False)
    st.download_button(
        f"Exportar {nome.replace('_', ' ')}",
        data=buf.getvalue(),
        file_name=f"{nome.replace(' ','_')}_{agora_br().strftime('%Y%m%d_%H%M')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"export_{nome}",
    )

def _slug(txt: str) -> str:
    return re.sub(r'[^a-z0-9]', '', str(txt).lower())

def achar_aba(sheet_names: list, candidatos: list):
    mapa = {_slug(n): n for n in sheet_names}
    for cand in candidatos:
        if _slug(cand) in mapa:
            return mapa[_slug(cand)]
    return None

def achar_coluna(df: pd.DataFrame, candidatos: list):
    mapa = {_slug(c): c for c in df.columns}
    for cand in candidatos:
        if _slug(cand) in mapa:
            return mapa[_slug(cand)]
    return None

def achar_coluna_ou_erro(df: pd.DataFrame, candidatos: list, aba: str, descricao: str):
    c = achar_coluna(df, candidatos)
    if c is None:
        st.error(f"Aba '{aba}': não encontrei a coluna de {descricao}. "
                 f"Procurei por: {', '.join(candidatos)}. "
                 f"Colunas existentes: {', '.join(df.columns)}.")
        st.stop()
    return c

# ═══════════════════════════════════════════════════════════
# I/O
# ═══════════════════════════════════════════════════════════
def get_sheet_id() -> str:
    try:
        url = st.secrets["google_sheets"]["url"]
    except KeyError:
        st.error("Configuração ausente: [google_sheets] url em secrets.toml.")
        st.stop()
    m = re.search(r"/d/([a-zA-Z0-9-_]+)", url)
    if not m:
        st.error("URL do Google Sheets inválida.")
        st.stop()
    return m.group(1)

def get_credentials():
    scopes = ['https://www.googleapis.com/auth/spreadsheets',
              'https://www.googleapis.com/auth/drive.readonly']
    if "gcp_service_account" not in st.secrets:
        st.error("Credenciais (gcp_service_account) não configuradas em secrets.toml.")
        st.stop()
    return Credentials.from_service_account_info(st.secrets["gcp_service_account"], scopes=scopes)

@st.cache_data(show_spinner=False, ttl=300)
def buscar_planilha_bytes() -> bytes:
    sheet_id = get_sheet_id()
    export_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx"
    creds = get_credentials()
    creds.refresh(Request())
    headers = {'Authorization': f'Bearer {creds.token}'}
    try:
        resp = requests.get(export_url, headers=headers, timeout=TIMEOUT_S)
        resp.raise_for_status()
        mb = len(resp.content) / (1024 * 1024)
        if mb > MAX_DOWNLOAD_MB:
            st.error(f"Arquivo excede {MAX_DOWNLOAD_MB}MB.")
            st.stop()
        return resp.content
    except requests.exceptions.HTTPError as e:
        st.error(f"Falha HTTP: {e}")
        st.stop()
    except requests.exceptions.RequestException as e:
        st.error(f"Timeout/SSL: {e}")
        st.stop()

def _ws_controle():
    gc = gspread.authorize(get_credentials())
    sh = gc.open_by_key(get_sheet_id())
    try:
        return sh.worksheet(ABA_CONTROLE)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=ABA_CONTROLE, rows=5000, cols=len(COLS_CONTROLE))
        ws.append_row(COLS_CONTROLE)
        return ws

@st.cache_data(show_spinner=False, ttl=30)
def carregar_controle() -> pd.DataFrame:
    df_vazio = pd.DataFrame(columns=COLS_CONTROLE)
    try:
        ws = _ws_controle()
        # numericise_ignore=['all']: sem isso o gspread converte '01438784001330'
        # em número e o CNPJ perde o zero à esquerda — a nota nunca casava com o SAP.
        dados = ws.get_all_records(numericise_ignore=['all'])
        df = pd.DataFrame(dados) if dados else df_vazio
        if 'NF' not in df.columns and 'Chave' in df.columns:
            df = df.rename(columns={'Chave': 'NF'})
        if 'CNPJ' not in df.columns:
            df['CNPJ'] = ''
        return df
    except Exception as e:
        st.warning(f"gspread falhou: {e}")
        return df_vazio

def _chave_ctrl(nf, cnpj) -> str:
    return norm_nf_val(nf) + '|' + norm_cnpj_val(cnpj)

def salvar_controle(registros: list) -> bool:
    """Grava na aba Controle SÓ as notas alteradas: atualiza a linha da nota se
    ela já existe, senão acrescenta no fim. Antes a aba inteira era apagada e
    reescrita a partir de uma cópia em cache — se a escrita falhasse no meio a
    aba ficava vazia, e duas pessoas salvando perto uma da outra sobrescreviam
    o trabalho uma da outra. Campos ausentes no registro mantêm o valor atual."""
    try:
        ws = _ws_controle()
        valores = ws.get_all_values()  # leitura fresca, sem cache
        cab = list(valores[0]) if valores else []
        cab_mudou = not valores
        if 'NF' not in cab and 'Chave' in cab:
            cab[cab.index('Chave')] = 'NF'
            cab_mudou = True
        faltando = [c for c in COLS_CONTROLE if c not in cab]
        if faltando:
            cab += faltando
            cab_mudou = True
        pos = {c: i for i, c in enumerate(cab)}

        linha_da_chave = {}
        for n, lin in enumerate(valores[1:], start=2):
            lin = list(lin) + [''] * (len(cab) - len(lin))
            linha_da_chave[_chave_ctrl(lin[pos['NF']], lin[pos['CNPJ']])] = (n, lin)

        # uma entrada por nota (o último registro da mesma nota vence)
        por_chave = {}
        for reg in registros:
            por_chave[_chave_ctrl(reg['NF'], reg['CNPJ'])] = reg

        atualizar, novas = [], []
        for k, reg in por_chave.items():
            n, lin = linha_da_chave.get(k, (None, [''] * len(cab)))
            lin = list(lin)
            for c, v in reg.items():
                lin[pos[c]] = '' if v is None else str(v)
            if n:
                atualizar.append({'range': f'A{n}', 'values': [lin]})
            else:
                novas.append(lin)

        if cab_mudou:
            ws.update(range_name='A1', values=[cab])
        if atualizar:
            ws.batch_update(atualizar)
        if novas:
            ws.append_rows(novas)
        carregar_controle.clear()
        st.success(f"Controle salvo na planilha ({len(por_chave)} nota(s)).")
        return True
    except Exception as e:
        st.error(f"Falha ao salvar controle: {e}")
        return False

# ═══════════════════════════════════════════════════════════
# PROCESSAMENTO  —  CRUZAMENTO POR CHAVE DE ACESSO
# ═══════════════════════════════════════════════════════════
@st.cache_data(show_spinner=False)
def _parse_planilha(file_bytes: bytes):
    """Abre o workbook e lê TODAS as abas usadas de uma vez só (SAP, Qive se
    existir, e NFs). Cada 'pd.read_excel' reprocessa a tabela de strings
    compartilhadas do ARQUIVO INTEIRO mesmo pra ler uma aba só — e como a aba
    NFs ficou enorme (formato Arquivei), isso sozinho já é lento. Abrir na mão
    3-4 vezes (SAP, tentativa de Qive, NFs, de novo NFs) era o que travava
    o app. Agora só se abre uma vez, cacheado por conteúdo do arquivo."""
    xls = pd.ExcelFile(io.BytesIO(file_bytes), engine='openpyxl')
    sheet_names = list(xls.sheet_names)

    df_sap = pd.read_excel(xls, sheet_name='SAP', dtype=str) if 'SAP' in sheet_names else pd.DataFrame()

    aba_qive = achar_aba(sheet_names, ['Qive', 'QIVE'])
    df_qive = pd.read_excel(xls, sheet_name=aba_qive, dtype=str) if aba_qive else pd.DataFrame()

    aba_nfs = achar_aba(sheet_names, ABA_NFS_CAND)
    df_nfs = pd.read_excel(xls, sheet_name=aba_nfs, dtype=str) if aba_nfs else pd.DataFrame()

    return {
        'sheet_names': sheet_names,
        'df_sap': df_sap,
        'df_qive': df_qive, 'aba_qive': aba_qive,
        'df_nfs': df_nfs, 'aba_nfs': aba_nfs,
    }

def _centro_custo_index(df_nfs: pd.DataFrame):
    """Constrói {nf|cnpj: 'CC1 | CC2'} a partir da aba NFs."""
    try:
        if df_nfs is None or df_nfs.empty:
            return {}
        col_nf   = achar_coluna(df_nfs, COL_NF_NFS_CAND)
        col_cnpj = achar_coluna(df_nfs, COL_CNPJ_NFS_CAND)
        col_cc   = achar_coluna(df_nfs, COL_CC_CAND)
        if col_nf is None or col_cc is None:
            return {}
        idx = {}
        for _, r in df_nfs.iterrows():
            nf = norm_nf_val(r.get(col_nf, ''))
            cn = norm_cnpj_val(r.get(col_cnpj, '')) if col_cnpj else ''
            cc = str(r.get(col_cc, '')).strip()
            if not nf or not cc or cc.lower() in LIXO:
                continue
            k = nf + '|' + cn
            idx.setdefault(k, [])
            if cc not in idx[k]:
                idx[k].append(cc)
        return {k: ' | '.join(v) for k, v in idx.items()}
    except Exception:
        return {}

def _mapa_centro_custo(df_nfs: pd.DataFrame, df_merge: pd.DataFrame) -> pd.Series:
    idx = _centro_custo_index(df_nfs)
    if not idx:
        return pd.Series('', index=df_merge.index)
    nf = df_merge.get('Nº NF', pd.Series('', index=df_merge.index)).map(norm_nf_val)
    cn = df_merge.get('CNPJ Emitente', pd.Series('', index=df_merge.index)).map(norm_cnpj_val)
    chaves = nf + '|' + cn
    return chaves.map(lambda k: idx.get(k, ''))

def _nfs_index(df_nfs: pd.DataFrame):
    """Índices da aba NFs POR FORNECEDOR: {cnpj: {'nf': set, 'vals': set}} e
    {nome_limpo: {'nf': set, 'vals': set}}. Assim NF/valor só contam se
    estiverem na mesma linha do fornecedor."""
    try:
        if df_nfs is None or df_nfs.empty:
            return None
        col_nf   = achar_coluna(df_nfs, COL_NF_NFS_CAND)
        col_cnpj = achar_coluna(df_nfs, COL_CNPJ_NFS_CAND)
        col_nome = achar_coluna(df_nfs, COL_NOME_NFS_CAND)
        col_va   = achar_coluna(df_nfs, ['Total da linha'])
        col_vb   = achar_coluna(df_nfs, ['Total documento'])
        por_cnpj, por_nome = {}, {}
        for _, r in df_nfs.iterrows():
            nf   = norm_nf_val(r[col_nf]) if col_nf else ''
            vals = {v for v in (norm_valor(r[c]) for c in (col_va, col_vb) if c) if v is not None}
            alvos = []
            if col_cnpj:
                cn = norm_cnpj_val(r[col_cnpj])
                if cn:
                    alvos.append(por_cnpj.setdefault(cn, {'nf': set(), 'vals': set()}))
            if col_nome:
                nome = _limpa_nome(r[col_nome])
                if nome:
                    alvos.append(por_nome.setdefault(nome, {'nf': set(), 'vals': set()}))
            for a in alvos:
                if nf and nf.lower() not in LIXO:
                    a['nf'].add(nf)
                a['vals'] |= vals
        return {'cnpj': por_cnpj, 'nome': por_nome}
    except Exception:
        return None

def _nomes_limpos_parecidos(na: str, nb: str) -> bool:
    """Mesma regra de _nomes_parecidos, mas recebendo nomes JÁ limpos e sem
    tratar nome vazio como 'parecido'."""
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    sa, sb = set(na.split()), set(nb.split())
    return bool(sa and sb and len(sa & sb) / min(len(sa), len(sb)) >= 0.6)

def _presenca_nfs(df_nfs: pd.DataFrame, df_merge: pd.DataFrame) -> pd.Series:
    """Marca True quando a nota aparece na aba NFs: o fornecedor (CNPJ; se não
    achar, nome parecido) precisa ter, NAS SUAS PRÓPRIAS LINHAS, o mesmo Nº NF
    ou o mesmo valor. Antes bastava o fornecedor existir em qualquer linha e o
    número existir em qualquer outra — NFs como 4, 7 ou 8 sempre 'batiam'."""
    idx = _nfs_index(df_nfs)
    if not idx:
        return pd.Series(False, index=df_merge.index)

    def _bate(grupo, nf, val):
        return (nf and nf in grupo['nf']) or (val is not None and val in grupo['vals'])

    res = []
    for _, r in df_merge.iterrows():
        nf   = norm_nf_val(r.get('Nº NF', ''))
        cn   = norm_cnpj_val(r.get('CNPJ Emitente', ''))
        val  = norm_valor(r.get('Valor Nota Fiscal', ''))
        ok = bool(cn and cn in idx['cnpj'] and _bate(idx['cnpj'][cn], nf, val))
        if not ok:
            nome = _limpa_nome(r.get('Razão Social', ''))
            ok = any(_nomes_limpos_parecidos(nome, nb) and _bate(g, nf, val)
                     for nb, g in idx['nome'].items())
        res.append(ok)
    return pd.Series(res, index=df_merge.index)

@st.cache_data(show_spinner=False)
def processar(file_bytes: bytes):
    dados = _parse_planilha(file_bytes)
    df_sap  = dados['df_sap'].copy()
    df_qive = dados['df_qive'].copy()
    aba_qive_existe = dados['aba_qive'] is not None and not df_qive.empty
    if df_sap.empty:
        st.error("Aba 'SAP' não encontrada na planilha.")
        st.stop()

    # Colunas (tolerante)
    col_nf_sap    = achar_coluna_ou_erro(df_sap,  COL_NF_SAP_CAND,   'SAP',  'número da NF ("Nº NF")')
    col_cnpj_sap  = achar_coluna_ou_erro(df_sap,  COL_CNPJ_SAP_CAND, 'SAP',  'CNPJ/CPF emitente')
    if aba_qive_existe:
        col_nf_qive   = achar_coluna_ou_erro(df_qive, COL_NF_QIVE_CAND,  'Qive', 'número da NF ("Número")')
        col_cnpj_qive = achar_coluna(df_qive, COL_CNPJ_QIVE_CAND)
        col_cpf_qive  = achar_coluna(df_qive, COL_CPF_QIVE_CAND)
    else:
        col_nf_qive = col_cnpj_qive = col_cpf_qive = None

    for c in ['Situação Manifestação', 'Tipo Doc. Fiscal', 'Chave de Acesso']:
        if c not in df_sap.columns:
            st.error(f"Aba 'SAP' está sem a coluna obrigatória: {c}.")
            st.stop()
    if aba_qive_existe and 'Chave de acesso' not in df_qive.columns:
        st.error("Aba 'Qive' está sem a coluna obrigatória: Chave de acesso.")
        st.stop()

    # Número e chave canônicos
    df_sap['Nº NF']       = norm_nf(df_sap[col_nf_sap])
    df_sap['_chave']      = norm_chave(df_sap['Chave de Acesso'])
    df_sap['Chave de Acesso'] = normalizar(df_sap['Chave de Acesso'])

    # CNPJ do SAP
    df_sap['CNPJ Emitente'] = norm_cnpj(df_sap[col_cnpj_sap])

    # Qive: número, chave e documento (CNPJ; se vazio, CPF)
    if aba_qive_existe:
        df_qive['Número NF'] = norm_nf(df_qive[col_nf_qive])
        df_qive['_chave']    = norm_chave(df_qive['Chave de acesso'])
        doc_q = norm_cnpj(df_qive[col_cnpj_qive]) if col_cnpj_qive else pd.Series('', index=df_qive.index)
        if col_cpf_qive is not None:
            cpf_q = norm_cnpj(df_qive[col_cpf_qive])
            doc_q = doc_q.where(doc_q.str.len() > 0, cpf_q)
        df_qive['CNPJ NF'] = doc_q
    else:
        df_qive = pd.DataFrame(columns=['Número NF', '_chave', 'CNPJ NF', 'Chave de acesso'])

    # Filtro base (escopo)
    mask_nfe = df_sap['Tipo Doc. Fiscal'].astype(str).str.strip().str.lower() == 'nf-e'
    mask_sit = df_sap['Situação Manifestação'].astype(str).str.strip().str.lower().isin(SITUACOES_VALIDAS)
    df_no_escopo   = df_sap[mask_nfe & mask_sit].copy()
    df_fora_escopo = df_sap[~(mask_nfe & mask_sit)].copy()

    # Exclusões
    mask_excl = pd.Series(False, index=df_no_escopo.index)
    for col in COLUNAS_EXCLUSAO:
        if col in df_no_escopo.columns:
            mask_excl |= df_no_escopo[col].astype(str).str.contains(TERMOS_EXCLUSAO, case=False, na=False)
    # CFOP de retorno/devolução/conserto sai do escopo
    col_cfop = achar_coluna(df_no_escopo, COL_CFOP_CAND)
    if col_cfop is not None:
        cfop_norm = (df_no_escopo[col_cfop].astype(str)
                     .str.replace(r'\D', '', regex=True).str.strip())
        mask_excl |= cfop_norm.isin(CFOP_RETORNO_DEVOLUCAO)
    df_excluidos = df_no_escopo[mask_excl].copy()
    df_ativo     = df_no_escopo[~mask_excl].copy()

    # SAP às vezes traz a mesma nota em mais de uma linha (mesma chave de
    # acesso); sem isso ela vira duas pendências. Feito depois da regra de
    # exclusão para não perder um CFOP/natureza que só aparece numa das linhas.
    def _sem_chave_repetida(df):
        tem = df['_chave'].str.len() > 0
        return df[~(tem & df['_chave'].duplicated(keep='first'))].copy()
    df_ativo       = _sem_chave_repetida(df_ativo)
    df_excluidos   = _sem_chave_repetida(df_excluidos)
    df_fora_escopo = _sem_chave_repetida(df_fora_escopo)

    # Qive: marca duplicatas de chave e dedup (mantém 1)
    df_qive_u = df_qive.copy()
    df_qive_u = df_qive_u[df_qive_u['_chave'].str.len() > 0].copy()
    dup_chaves = set(df_qive_u['_chave'][df_qive_u['_chave'].duplicated(keep=False)])
    df_qive_u['_dup_qive'] = df_qive_u['_chave'].isin(dup_chaves)
    df_qive_u = df_qive_u.drop_duplicates(subset='_chave', keep='first')

    # IMPORTANTE: reduz a Qive APENAS às colunas que vamos usar e renomeia com
    # sufixo _qive, para NÃO colidir com colunas de mesmo nome no SAP (ex.: a
    # Qive também tem 'CNPJ Emitente'). Sem isso o merge cria _x/_y e o CNPJ
    # do SAP "some", quebrando o double-check.
    cols_qive = {'_chave': '_chave', 'Número NF': '_nf_qive', 'CNPJ NF': '_cnpj_qive',
                 'Nome/Razão Social Emitente': '_razao_qive', 'Chave de acesso': '_chave_qive',
                 'Manifestação': '_manif_qive', 'Data Manifestação': '_manifdata_qive',
                 '_dup_qive': '_dup_qive'}
    cols_existentes = {k: v for k, v in cols_qive.items() if k in df_qive_u.columns}
    df_qive_u = df_qive_u[list(cols_existentes.keys())].rename(columns=cols_existentes)

    # ── CRUZAMENTO POR CHAVE DE ACESSO (base = SAP) ──
    # how='left': mantém apenas as notas que existem no SAP; a Qive só confirma.
    df_merge = pd.merge(df_ativo, df_qive_u, on='_chave', how='left', indicator=True)

    # Guarda os valores ORIGINAIS do SAP antes de qualquer coalescência,
    # para o double-check SAP x Qive (NF + nome + CNPJ).
    nf_sap_orig   = df_merge.get('Nº NF', pd.Series('', index=df_merge.index)).map(norm_nf_val)
    cnpj_sap_orig = df_merge.get('CNPJ Emitente', pd.Series('', index=df_merge.index)).map(norm_cnpj_val)
    razao_sap_orig= df_merge.get('Razão Social', pd.Series('', index=df_merge.index)).astype(str)
    nf_qive_v     = df_merge.get('_nf_qive', pd.Series('', index=df_merge.index)).map(norm_nf_val)
    cnpj_qive_v   = df_merge.get('_cnpj_qive', pd.Series('', index=df_merge.index)).map(norm_cnpj_val)
    razao_qive_v  = df_merge.get('_razao_qive', pd.Series('', index=df_merge.index)).astype(str)
    tem_qive      = (df_merge['_merge'] == 'both')

    # Nº NF: vem do SAP; nos raros casos sem NF no SAP, cai pro número da Qive
    nf_sap  = df_merge.get('Nº NF', pd.Series(dtype=str))
    nf_qive = df_merge.get('_nf_qive', pd.Series('', index=df_merge.index))
    df_merge['Nº NF'] = nf_sap.replace(['nan','None',np.nan,''], pd.NA).fillna(nf_qive)

    # CNPJ Emitente: SEMPRE o do SAP (já é a base); fallback pro da Qive se vazio
    cn_sap  = df_merge.get('CNPJ Emitente', pd.Series('', index=df_merge.index))
    cn_qive = df_merge.get('_cnpj_qive', pd.Series('', index=df_merge.index))
    df_merge['CNPJ Emitente'] = cn_sap.replace(['nan','None',np.nan,''], pd.NA).fillna(cn_qive)

    # Razão Social: SAP; fallback Qive
    em_sap  = df_merge.get('Razão Social', pd.Series('', index=df_merge.index))
    em_qive = df_merge.get('_razao_qive', pd.Series('', index=df_merge.index))
    df_merge['Razão Social'] = em_sap.replace(['nan','None',np.nan,''], pd.NA).fillna(em_qive)

    # ── DOUBLE-CHECK SAP x Qive: NF + nome do fornecedor + CNPJ ──
    conf_sq = []
    for i in range(len(df_merge)):
        if not tem_qive.iloc[i]:
            conf_sq.append('— (sem par na Qive)')
            continue
        divergencias = []
        if nf_sap_orig.iloc[i] and nf_qive_v.iloc[i] and nf_sap_orig.iloc[i] != nf_qive_v.iloc[i]:
            divergencias.append('Nº NF')
        if cnpj_sap_orig.iloc[i] and cnpj_qive_v.iloc[i] and cnpj_sap_orig.iloc[i] != cnpj_qive_v.iloc[i]:
            divergencias.append('CNPJ')
        if not _nomes_parecidos(razao_sap_orig.iloc[i], razao_qive_v.iloc[i]):
            divergencias.append('Nome')
        conf_sq.append('✅ Confere (NF, nome e CNPJ)' if not divergencias
                       else '⚠️ Diverge: ' + ', '.join(divergencias))
    df_merge['Conferência SAP×Qive'] = conf_sq

    # Chave de Acesso já é a do SAP (coluna mantida); nada a coalescer

    # Manifestação da Qive (legível) + conferência com o SAP
    df_merge['Manifestação Qive'] = df_merge.get('_manif_qive', pd.Series('', index=df_merge.index)).fillna('')
    df_merge['Data Manif. Qive']  = df_merge.get('_manifdata_qive', pd.Series('', index=df_merge.index)).fillna('')
    sit_sap = df_merge.get('Situação Manifestação', pd.Series('', index=df_merge.index)).astype(str)
    man_qiv = df_merge['Manifestação Qive'].astype(str)
    df_merge['Conferência Manifestação'] = [
        _conferir_manifestacao(a, b) for a, b in zip(sit_sap, man_qiv)
    ]

    # ── CENTRO DE CUSTO (vem da aba NFs, casando por NF + CNPJ) ──
    df_merge['Centro de Custo'] = _mapa_centro_custo(dados['df_nfs'], df_merge)

    # ── PRESENÇA NAS 3 ABAS (SAP × Qive × NFs) ──
    # Base é o SAP; 'na Qive' vem do merge; 'na NFs' por nome/CNPJ/NF/valor.
    na_qive = (df_merge['_merge'] == 'both')
    na_nfs  = _presenca_nfs(dados['df_nfs'], df_merge)
    df_merge['Na Qive'] = na_qive.map({True: 'Sim', False: 'Não'})
    df_merge['Na NFs']  = na_nfs.map({True: 'Sim', False: 'Não'})
    df_merge['Nas 3 abas'] = (na_qive & na_nfs).map({True: '✅ Sim (SAP+Qive+NFs)', False: '⚠️ Não'})

    df_merge['Status Cruzamento'] = df_merge['_merge'].map({
        'left_only':  'No SAP — não encontrada na Qive',
        'both':       'OK (SAP e Qive)',
    })
    # Anexa marca de duplicado na Qive ao status
    if '_dup_qive' in df_merge.columns:
        dupm = df_merge['_dup_qive'].fillna(False)
        df_merge.loc[dupm, 'Status Cruzamento'] = df_merge.loc[dupm, 'Status Cruzamento'].astype(str) + ' • duplicado na Qive'

    # Pendências (todas as notas já são base SAP)
    df_sap_lado = df_merge.copy()
    mask_pend   = df_sap_lado.get('Situação Manifestação', pd.Series(dtype=str)).astype(str).str.contains('não informado', case=False, na=False)
    if 'Manifestação' in df_sap_lado.columns:
        mask_pend |= df_sap_lado['Manifestação'].astype(str).str.contains('sem manifestação', case=False, na=False)
    df_pend = df_sap_lado[mask_pend].copy()

    mask_excl_pend = pd.Series(False, index=df_pend.index)
    for col in COLUNAS_EXCLUSAO:
        if col in df_pend.columns:
            mask_excl_pend |= df_pend[col].astype(str).str.contains(TERMOS_EXCLUSAO, case=False, na=False)
    df_pend = df_pend[~mask_excl_pend].copy()

    return limpar_display(df_merge), limpar_display(df_pend), limpar_display(df_fora_escopo), limpar_display(df_excluidos)

@st.cache_data(show_spinner=False)
def carregar_nfs(file_bytes: bytes):
    try:
        dados = _parse_planilha(file_bytes)
        df, aba, sheet_names = dados['df_nfs'], dados['aba_nfs'], dados['sheet_names']
        vazio = {'col_nf': None, 'col_cnpj': None, 'col_desc': None,
                 'col_nome': None, 'col_val_a': None, 'col_val_b': None}
        if aba is None:
            return pd.DataFrame(), vazio, None, sheet_names, None
        cols = {
            'col_nf':   achar_coluna(df, COL_NF_NFS_CAND),
            'col_cnpj': achar_coluna(df, COL_CNPJ_NFS_CAND),
            'col_desc': achar_coluna(df, COL_DESC_CAND),
            'col_nome': achar_coluna(df, COL_NOME_NFS_CAND),
            'col_val_a': achar_coluna(df, ['Total da linha']),
            'col_val_b': achar_coluna(df, ['Total documento']),
        }
        return limpar_display(df), cols, aba, sheet_names, None
    except Exception as e:
        vazio = {'col_nf': None, 'col_cnpj': None, 'col_desc': None,
                 'col_nome': None, 'col_val_a': None, 'col_val_b': None}
        return pd.DataFrame(), vazio, None, [], str(e)

def filtrar_nfs(df_nfs, col_nf, col_cnpj, nf, cnpj):
    """Filtra por NF e faz double-check por CNPJ. Se o CNPJ não bate, retorna vazio."""
    if df_nfs is None or df_nfs.empty or col_nf is None:
        return pd.DataFrame()
    sel = df_nfs[df_nfs[col_nf].map(norm_nf_val) == norm_nf_val(nf)].copy()
    if sel.empty:
        return sel
    cnpj_alvo = norm_cnpj_val(cnpj)
    if col_cnpj is not None and cnpj_alvo:
        sel_cnpj = sel[sel[col_cnpj].map(norm_cnpj_val) == cnpj_alvo]
        return sel_cnpj  # vazio se não confere -> não mostra nada
    return sel

@st.cache_data(show_spinner=False)
def _descricao_index(df_nfs: pd.DataFrame, col_nf: str, col_cnpj, col_desc: str):
    """Índice {(nf,cnpj): 'descrições'} e {nf: 'descrições'} construído
    UMA ÚNICA VEZ. Antes, cada nota da tabela principal escaneava a aba NFs
    inteira de novo — com centenas de notas x milhares de linhas da NFs,
    isso é o que deixava a tela lentíssima."""
    idx_key, idx_nf = {}, {}
    if df_nfs is None or df_nfs.empty or not col_desc or col_desc not in df_nfs.columns or not col_nf:
        return idx_key, idx_nf
    nf_norm = df_nfs[col_nf].map(norm_nf_val)
    if col_cnpj and col_cnpj in df_nfs.columns:
        cn_norm = df_nfs[col_cnpj].map(norm_cnpj_val)
    else:
        cn_norm = pd.Series('', index=df_nfs.index)
    desc_col = df_nfs[col_desc].astype(str)
    for i in df_nfs.index:
        nf = nf_norm.at[i]
        if not nf:
            continue
        d = desc_col.at[i].strip()
        if not d or d.lower() in LIXO:
            continue
        cn = cn_norm.at[i]
        for chave, mapa in ((nf, idx_nf), ((nf, cn), idx_key)):
            lst = mapa.setdefault(chave, [])
            if d not in lst:
                lst.append(d)
    idx_key = {k: ' | '.join(v) for k, v in idx_key.items()}
    idx_nf  = {k: ' | '.join(v) for k, v in idx_nf.items()}
    return idx_key, idx_nf

def descricao_da_nf(df_nfs, cols_nfs, nf, cnpj, nome_nota, valor_nota) -> str:
    """Traz a descrição do item/serviço (coluna E da aba NF's) — o campo que explica
    do que se trata a nota.
    1) Casamento EXATO por Nº NF + CNPJ Emitente (mesma chave usada no cruzamento
       SAP × NFs — é o jeito mais confiável de achar a nota certa), via índice
       pré-calculado (O(1) por nota, em vez de escanear a aba inteira toda vez).
    2) Se não achar nada por aí, cai para o casamento por NOME do fornecedor + VALOR
       exato (fallback antigo, útil quando a nota não tem NF/CNPJ correspondente)."""
    col_desc = cols_nfs.get('col_desc')
    if df_nfs is None or df_nfs.empty or col_desc is None or col_desc not in df_nfs.columns:
        return ""

    col_nf, col_cnpj = cols_nfs.get('col_nf'), cols_nfs.get('col_cnpj')
    idx_key, idx_nf = _descricao_index(df_nfs, col_nf, col_cnpj, col_desc)
    nf_n = norm_nf_val(nf)
    if nf_n:
        cn_n = norm_cnpj_val(cnpj)
        if (nf_n, cn_n) in idx_key:
            return idx_key[(nf_n, cn_n)]
        if nf_n in idx_nf:
            return idx_nf[nf_n]

    def _unicas(serie: pd.Series) -> str:
        descricoes = [d.strip() for d in serie.astype(str).tolist()
                      if d.strip() and d.strip().lower() not in LIXO]
        vistos, out = set(), []
        for d in descricoes:
            if d not in vistos:
                vistos.add(d); out.append(d)
        return " | ".join(out)

    # 2) Fallback: nome do fornecedor + valor exato
    col_nome = cols_nfs.get('col_nome')
    col_val_a, col_val_b = cols_nfs.get('col_val_a'), cols_nfs.get('col_val_b')
    val = norm_valor(valor_nota)
    if col_nome is None or val is None or not str(nome_nota).strip():
        return ""
    mask_nome = df_nfs[col_nome].map(lambda x: _nomes_parecidos(x, nome_nota))
    cand = df_nfs[mask_nome]
    if cand.empty:
        return ""
    bate = pd.Series(False, index=cand.index)
    if col_val_a and col_val_a in cand.columns:
        bate |= cand[col_val_a].map(norm_valor) == val
    if col_val_b and col_val_b in cand.columns:
        bate |= cand[col_val_b].map(norm_valor) == val
    det = cand[bate]
    return _unicas(det[col_desc]) if not det.empty else ""

# ═══════════════════════════════════════════════════════════
# INTERFACE
# ═══════════════════════════════════════════════════════════
def montar_tabela_editavel(df: pd.DataFrame, df_controle: pd.DataFrame, df_nfs=None, cols_nfs=None) -> pd.DataFrame:
    cols = [c for c in COLUNAS_EXIBIR if c in df.columns]
    df_view = df[cols].copy()

    # "Do que se trata" — via índice pré-calculado (uma vez só), lookup O(1) por linha
    if df_nfs is not None and cols_nfs is not None and 'Nº NF' in df.columns:
        idx_key, idx_nf = _descricao_index(df_nfs, cols_nfs.get('col_nf'),
                                            cols_nfs.get('col_cnpj'), cols_nfs.get('col_desc'))
        nf_col  = df['Nº NF'].map(norm_nf_val) if 'Nº NF' in df.columns else pd.Series('', index=df.index)
        cn_col  = df['CNPJ Emitente'].map(norm_cnpj_val) if 'CNPJ Emitente' in df.columns else pd.Series('', index=df.index)

        def _lookup(nf_n, cn_n):
            if not nf_n:
                return ""
            if (nf_n, cn_n) in idx_key:
                return idx_key[(nf_n, cn_n)]
            return idx_nf.get(nf_n, "")

        descricoes = [
            _lookup(nf_n, cn_n) or descricao_da_nf(
                df_nfs, cols_nfs, df.at[i, 'Nº NF'], df.at[i, 'CNPJ Emitente'],
                df.at[i, 'Razão Social'] if 'Razão Social' in df.columns else '',
                df.at[i, 'Valor Nota Fiscal'] if 'Valor Nota Fiscal' in df.columns else '',
            )
            for i, nf_n, cn_n in zip(df.index, nf_col, cn_col)
        ]
        df_view.insert(min(5, len(df_view.columns)), 'Do que se trata', descricoes)

    if not df_controle.empty and {'NF', 'CNPJ'}.issubset(df_controle.columns) and 'Nº NF' in df_view.columns:
        df_ctrl = df_controle[['NF', 'CNPJ', 'Tratado', 'Observacao_Controle']].copy()
        df_ctrl['_k'] = df_ctrl['NF'].map(norm_nf_val) + '|' + df_ctrl['CNPJ'].map(norm_cnpj_val)
        cnpj_series = df_view['CNPJ Emitente'] if 'CNPJ Emitente' in df_view.columns else pd.Series('', index=df_view.index)
        df_view['_k'] = df_view['Nº NF'].map(norm_nf_val) + '|' + cnpj_series.map(norm_cnpj_val)
        df_view = df_view.merge(df_ctrl[['_k', 'Tratado', 'Observacao_Controle']], on='_k', how='left')
        df_view.drop(columns=['_k'], inplace=True, errors='ignore')
        df_view['Tratado'] = df_view['Tratado'].fillna('').map(lambda x: x == 'True' or x is True)
        df_view['Observacao_Controle'] = df_view['Observacao_Controle'].fillna('')
    else:
        df_view['Tratado'] = False
        df_view['Observacao_Controle'] = ''

    col_order = ['Tratado'] + [c for c in df_view.columns if c not in ('Tratado', 'Observacao_Controle')] + ['Observacao_Controle']
    return df_view[[c for c in col_order if c in df_view.columns]]

TODAS_SIT, TODOS_CC, TODAS_ABAS = "Todas as situações", "Todos os centros de custo", "Presença: todas"

def filtros(df: pd.DataFrame, key: str) -> pd.DataFrame:
    tem_cc  = 'Centro de Custo' in df.columns and df['Centro de Custo'].astype(str).str.strip().ne('').any()
    tem_3   = 'Nas 3 abas' in df.columns

    # monta o número de colunas conforme os filtros disponíveis
    pesos = [3, 2] + ([2] if tem_cc else []) + ([2] if tem_3 else []) + [1]
    cols = st.columns(pesos)
    ci = iter(cols)
    col_busca  = next(ci)
    col_status = next(ci)
    col_cc     = next(ci) if tem_cc else None
    col_3      = next(ci) if tem_3 else None
    col_reset  = next(ci)

    with col_busca:
        busca = st.text_input("busca", key=f"busca_{key}",
                              placeholder="Buscar por Nº NF (exato), CNPJ, emitente ou chave…",
                              label_visibility="collapsed")
    with col_status:
        opts = [TODAS_SIT]
        if 'Situação Manifestação' in df.columns:
            opts += sorted(df['Situação Manifestação'].dropna().unique().tolist())
        sel = st.selectbox("Situação", opts, key=f"status_{key}", label_visibility="collapsed")

    cc_sel = TODOS_CC
    if tem_cc:
        todos_cc = set()
        for v in df['Centro de Custo'].dropna().astype(str):
            for parte in v.split('|'):
                p = parte.strip()
                if p:
                    todos_cc.add(p)
        with col_cc:
            cc_sel = st.selectbox("Centro de custo", [TODOS_CC] + sorted(todos_cc),
                                  key=f"cc_{key}", label_visibility="collapsed")

    tres_sel = TODAS_ABAS
    if tem_3:
        with col_3:
            tres_sel = st.selectbox("Presença nas abas",
                                    [TODAS_ABAS, "Só nas 3 abas", "Falta em alguma"],
                                    key=f"tres_{key}", label_visibility="collapsed")

    with col_reset:
        if st.button("Limpar", key=f"reset_{key}", use_container_width=True):
            for k in [f"busca_{key}", f"status_{key}", f"cc_{key}", f"tres_{key}"]:
                st.session_state.pop(k, None)
            st.rerun()

    res = df.copy()
    busca = busca.strip()
    if busca and re.fullmatch(r'\d{1,9}', busca) and 'Nº NF' in res.columns:
        # só dígitos e até 9 (tamanho máximo de um nº de NF): busca a NF exata,
        # senão '3459' também trazia notas com 3459 no meio do CNPJ ou da chave
        res = res[res['Nº NF'].map(norm_nf_val).str.lstrip('0') == busca.lstrip('0')]
    elif busca:
        mask = pd.Series(False, index=res.index)
        for col in ['Nº NF', 'CNPJ Emitente', 'Chave de Acesso', 'Razão Social']:
            if col in res.columns:
                mask |= res[col].astype(str).str.contains(busca, case=False, na=False, regex=False)
        res = res[mask]
    if sel != TODAS_SIT and 'Situação Manifestação' in res.columns:
        res = res[res['Situação Manifestação'] == sel]
    if cc_sel != TODOS_CC and 'Centro de Custo' in res.columns:
        res = res[res['Centro de Custo'].astype(str).str.contains(rf'(^|\|)\s*{re.escape(cc_sel)}\s*($|\|)', regex=True)]
    if tres_sel != TODAS_ABAS and 'Nas 3 abas' in res.columns:
        nas3 = res['Nas 3 abas'].astype(str).str.startswith('✅')
        res = res[nas3] if tres_sel == "Só nas 3 abas" else res[~nas3]
    return res

def _campo_html(label: str, valor: str, mono: bool = False) -> str:
    cls = 'cb-campo-valor cb-mono' if mono else 'cb-campo-valor'
    return (f'<div><div class="cb-campo-label">{html.escape(label)}</div>'
            f'<div class="{cls}">{valor}</div></div>')

def painel_detalhes(registro: pd.Series, nf, cnpj, df_nfs, cols_nfs, tratado: bool = False):
    def _v(c):
        val = str(registro.get(c, '')).strip()
        return '' if val.lower() in LIXO else val

    status = ('<span class="cb-badge cb-badge-ok">Tratado</span>' if tratado
              else '<span class="cb-badge cb-badge-pend">Pendente</span>')
    campos = [
        ('Nº NF', html.escape(_v('Nº NF')), True),
        ('CNPJ emitente', html.escape(fmt_doc(_v('CNPJ Emitente'))), True),
        ('Razão social', html.escape(_v('Razão Social')), False),
        ('Valor', html.escape(fmt_brl(_v('Valor Nota Fiscal'))), False),
        ('Situação manifestação', html.escape(_v('Situação Manifestação')), False),
        ('Manifestação Qive', html.escape(_v('Manifestação Qive')), False),
        ('Conferência SAP × Qive', html.escape(_v('Conferência SAP×Qive')), False),
        ('Centro de custo', html.escape(_v('Centro de Custo')), False),
        ('Presente na aba NFs', html.escape(_v('Na NFs')), False),
    ]
    itens = ''.join(_campo_html(l, v, m) for l, v, m in campos if v)
    chave = _v('Chave de Acesso')
    if chave:
        itens += _campo_html('Chave de acesso', html.escape(chave), mono=True)

    # "Do que se trata" — Descrição do item/serviço (coluna E da aba NF's)
    desc = descricao_da_nf(df_nfs, cols_nfs, nf, cnpj, _v('Razão Social'), _v('Valor Nota Fiscal'))
    desc_html = html.escape(desc).replace(' | ', '<br>') if desc else "Não encontrada na aba NF's."

    st.markdown(
        f'<div class="cb-card">'
        f'<div class="cb-card-titulo">NF {html.escape(_v("Nº NF"))} · {html.escape(_v("Razão Social"))} {status}</div>'
        f'<div class="cb-grid">{itens}</div>'
        f'<div class="cb-desc">{_campo_html("Do que se trata", desc_html)}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

def _tabela_exibicao(df_view: pd.DataFrame, key: str):
    """Cópia só para mostrar: R$, CNPJ com máscara e sem colunas redundantes.
    A lógica (salvar, lote, detalhes) continua usando o df_view original."""
    df_show = df_view.copy()
    if 'Valor Nota Fiscal' in df_show.columns:
        df_show['Valor Nota Fiscal'] = df_show['Valor Nota Fiscal'].map(fmt_brl)
    if 'CNPJ Emitente' in df_show.columns:
        df_show['CNPJ Emitente'] = df_show['CNPJ Emitente'].map(fmt_doc)
    # cada aba já separa tratados de não tratados, então a coluna só repetiria a aba
    # (e ficava parecendo uma segunda caixa de seleção ao lado da de selecionar linha)
    df_show = df_show.drop(columns=['Tratado'], errors='ignore')
    if key == 'pend':
        # em Pendências a situação é sempre "Não Informado" — é o que define a pendência
        df_show = df_show.drop(columns=['Situação Manifestação'], errors='ignore')
    config = {
        'Tratado': st.column_config.CheckboxColumn("Tratado", width="small"),
        'Nº NF': st.column_config.TextColumn("Nº NF", width="small"),
        'CNPJ Emitente': st.column_config.TextColumn("CNPJ emitente", width="medium"),
        'Razão Social': st.column_config.TextColumn("Razão social", width="large"),
        'Valor Nota Fiscal': st.column_config.TextColumn("Valor", width="medium"),
        'Situação Manifestação': st.column_config.TextColumn("Situação", width="small"),
        'Do que se trata': st.column_config.TextColumn("Do que se trata", width="large"),
        'Centro de Custo': st.column_config.TextColumn("Centro de custo", width="medium"),
        'Na NFs': st.column_config.TextColumn("Na NFs", width="small"),
        'Chave de Acesso': st.column_config.TextColumn("Chave de acesso", width="medium"),
        'Observacao_Controle': st.column_config.TextColumn("Observação", width="medium"),
    }
    return df_show, config

def render_editavel(df_dados: pd.DataFrame, df_controle: pd.DataFrame, df_nfs, cols_nfs,
                    key: str, nome_export: str, responsavel: str):
    df_filtrado = filtros(df_dados, key).reset_index(drop=True)
    st.caption(f"{len(df_filtrado)} registro(s) · clique numa linha para ver os detalhes, "
               "ou selecione várias para marcar em lote")

    df_view = montar_tabela_editavel(df_filtrado, df_controle, df_nfs, cols_nfs).reset_index(drop=True)
    df_show, col_config = _tabela_exibicao(df_view, key)

    event = st.dataframe(
        df_show,
        column_config=col_config,
        hide_index=True, use_container_width=True,
        height=min(420, 35 * (len(df_show) + 1) + 3),
        on_select="rerun", selection_mode="multi-row",
        key=f"sel_{key}",
    )

    # Linhas selecionadas (1 = controle + detalhes da nota; 2+ = ação em lote)
    try:
        rows = event.selection["rows"]
    except Exception:
        rows = getattr(getattr(event, "selection", None), "rows", []) or []
    rows = [r for r in rows if r < len(df_view)]
    sem_resp = not str(responsavel or '').strip()

    if len(rows) > 1:
        _barra_lote(df_view.iloc[rows], df_filtrado, key, nome_export, responsavel, sem_resp)
        return

    linha = df_view.iloc[rows[0]] if rows else None
    nf    = norm_nf_val(linha.get('Nº NF', '')) if linha is not None else ''
    cnpj  = norm_cnpj_val(linha.get('CNPJ Emitente', '')) if linha is not None else ''

    # ── BARRA SUPERIOR: Exportar + Controle desta nota (lado a lado) ──
    # key inclui a nota: sem isso o checkbox/observação ficavam com o valor da
    # nota selecionada antes ao trocar de linha.
    k_nota = f"{key}_{nf}_{cnpj}"
    b_exp, b_chk, b_obs, b_save = st.columns([2, 1.2, 4, 1.4])
    with b_exp:
        btn_exportar(df_filtrado, nome_export)
    with b_chk:
        novo_tratado = st.checkbox("Tratado",
                                   value=bool(linha.get('Tratado', False)) if linha is not None else False,
                                   key=f"chk_{k_nota}", disabled=(linha is None))
    with b_obs:
        nova_obs = st.text_input("Observação",
                                 value=str(linha.get('Observacao_Controle', '') or '') if linha is not None else '',
                                 key=f"obs_{k_nota}", disabled=(linha is None),
                                 label_visibility="collapsed", placeholder="Observação desta nota…")
    with b_save:
        salvar_click = st.button("Salvar", type="primary", key=f"savrow_{key}",
                                 disabled=(linha is None or sem_resp), use_container_width=True)

    if linha is None:
        return
    if sem_resp:
        st.warning("Preencha o **Responsável pela alteração** no topo da página para poder salvar.")

    # Registro completo (todas as colunas) para o painel
    registro = linha
    if 'Nº NF' in df_filtrado.columns and nf:
        cand = df_filtrado[df_filtrado['Nº NF'].map(norm_nf_val) == nf]
        if cnpj and 'CNPJ Emitente' in df_filtrado.columns:
            c2 = cand[cand['CNPJ Emitente'].map(norm_cnpj_val) == cnpj]
            if not c2.empty:
                cand = c2
        if not cand.empty:
            registro = cand.iloc[0]

    # Salvar (ação do botão da barra superior)
    if salvar_click:
        if not nf:
            st.error("Esta linha não tem Nº NF.")
            st.stop()
        ok = salvar_controle([{
            'NF': nf, 'CNPJ': cnpj, 'Tratado': novo_tratado,
            'Observacao_Controle': nova_obs, 'Responsavel': responsavel.strip(),
            'Atualizado_em': agora_br().strftime("%d/%m/%Y %H:%M"),
        }])
        if ok:
            st.rerun()

    painel_detalhes(registro, nf, cnpj, df_nfs, cols_nfs, tratado=bool(linha.get('Tratado', False)))

def _barra_lote(sel: pd.DataFrame, df_filtrado: pd.DataFrame, key: str, nome_export: str,
                responsavel: str, sem_resp: bool):
    """Marca/desmarca várias notas de uma vez, num único salvamento."""
    b_exp, b_info, b_obs, b_marca, b_desm = st.columns([2, 1.6, 3.4, 1.8, 1.4])
    with b_exp:
        btn_exportar(df_filtrado, nome_export)
    with b_info:
        st.markdown(f"**{len(sel)} notas selecionadas**")
    with b_obs:
        obs = st.text_input("Observação", key=f"obslote_{key}", label_visibility="collapsed",
                            placeholder="Observação p/ todas (vazio mantém a atual)…")
    with b_marca:
        marcar = st.button("Marcar tratadas", type="primary", key=f"lote_ok_{key}",
                           disabled=sem_resp, use_container_width=True)
    with b_desm:
        desmarcar = st.button("Desmarcar", key=f"lote_undo_{key}",
                              disabled=sem_resp, use_container_width=True)
    if sem_resp:
        st.warning("Preencha o **Responsável pela alteração** no topo da página para poder salvar.")
    if not (marcar or desmarcar):
        return

    agora = agora_br().strftime("%d/%m/%Y %H:%M")
    registros = []
    for _, linha in sel.iterrows():
        nf = norm_nf_val(linha.get('Nº NF', ''))
        if not nf or nf.lower() in LIXO:
            continue
        reg = {'NF': nf, 'CNPJ': norm_cnpj_val(linha.get('CNPJ Emitente', '')),
               'Tratado': bool(marcar), 'Responsavel': responsavel.strip(), 'Atualizado_em': agora}
        if obs.strip():
            reg['Observacao_Controle'] = obs.strip()
        registros.append(reg)
    if not registros:
        st.error("Nenhuma das linhas selecionadas tem Nº NF.")
        return
    if salvar_controle(registros):
        st.session_state.pop(f"sel_{key}", None)  # limpa a seleção depois de salvar
        st.rerun()

def render_detalhes_nf(df_nfs, cols_nfs, aba_real, sheet_names, erro):
    col_nf   = cols_nfs.get('col_nf')
    col_cnpj = cols_nfs.get('col_cnpj')
    st.markdown("##### Detalhes por nota fiscal (busca global)")
    if erro:
        st.error(f"Não consegui ler a aba de detalhes: {erro}")
        return
    if df_nfs is None or df_nfs.empty:
        st.warning("Não encontrei a aba de detalhes. "
                   f"Abas disponíveis: {', '.join(sheet_names) if sheet_names else '(nenhuma)'}.")
        return
    st.caption(f"Origem: aba **{aba_real}** ({len(df_nfs)} linha(s)). "
               f"Coluna NF: **{col_nf or '—'}** · Coluna CNPJ: **{col_cnpj or '—'}**")
    if col_nf is None:
        st.error(f"Não identifiquei a coluna de número. Colunas: {', '.join(df_nfs.columns)}.")
        st.dataframe(df_nfs.head(20), hide_index=True, use_container_width=True)
        return

    serie_nf = df_nfs[col_nf].map(norm_nf_val)
    numeros = sorted({n for n in serie_nf.unique() if n and n.lower() not in LIXO}, key=lambda x: (len(x), x))
    if not numeros:
        st.info("Não há números válidos na coluna.")
        return
    nf_sel = st.selectbox(f"Selecione o {col_nf}", options=numeros, key="nf_detalhe_sel",
                          placeholder="Digite ou escolha um número de NF…")
    if not nf_sel:
        return
    detalhe = df_nfs[serie_nf == nf_sel].copy()
    if col_cnpj is not None:
        cnpjs = sorted({norm_cnpj_val(c) for c in detalhe[col_cnpj].tolist() if norm_cnpj_val(c)})
        if len(cnpjs) > 1:
            st.warning(f"Essa NF aparece com {len(cnpjs)} CNPJs diferentes — selecione o emitente:")
            cnpj_sel = st.selectbox("CNPJ emitente", options=["Todos"] + cnpjs, key="nf_detalhe_cnpj")
            if cnpj_sel != "Todos":
                detalhe = detalhe[detalhe[col_cnpj].map(norm_cnpj_val) == cnpj_sel]
    st.markdown(f"**{len(detalhe)} linha(s)** para a NF `{nf_sel}`:")
    st.dataframe(detalhe, hide_index=True, use_container_width=True)
    btn_exportar(detalhe, f"NF_{nf_sel}")

def filtrar_tratados(df: pd.DataFrame, df_controle: pd.DataFrame) -> pd.DataFrame:
    """Retorna apenas as notas marcadas como Tratado na aba Controle."""
    if (df is None or df.empty or df_controle is None or df_controle.empty
            or not {'NF', 'CNPJ', 'Tratado'}.issubset(df_controle.columns)):
        return df.iloc[0:0].copy() if df is not None else pd.DataFrame()
    ctrl = df_controle.copy()
    trat = ctrl['Tratado'].map(lambda x: x is True or str(x).strip().lower() == 'true')
    chaves = set((ctrl.loc[trat, 'NF'].map(norm_nf_val) + '|'
                  + ctrl.loc[trat, 'CNPJ'].map(norm_cnpj_val)).tolist())
    if not chaves:
        return df.iloc[0:0].copy()
    cn = df['CNPJ Emitente'] if 'CNPJ Emitente' in df.columns else pd.Series('', index=df.index)
    k = df['Nº NF'].map(norm_nf_val) + '|' + cn.map(norm_cnpj_val)
    return df[k.isin(chaves)].copy()


def remover_tratados(df: pd.DataFrame, df_controle: pd.DataFrame) -> pd.DataFrame:
    """Remove do df as notas já marcadas como Tratado (ficam só na aba Tratados)."""
    if (df is None or df.empty or df_controle is None or df_controle.empty
            or not {'NF', 'CNPJ', 'Tratado'}.issubset(df_controle.columns)):
        return df
    trat = df_controle['Tratado'].map(lambda x: x is True or str(x).strip().lower() == 'true')
    chaves = set((df_controle.loc[trat, 'NF'].map(norm_nf_val) + '|'
                  + df_controle.loc[trat, 'CNPJ'].map(norm_cnpj_val)).tolist())
    if not chaves:
        return df
    cn = df['CNPJ Emitente'] if 'CNPJ Emitente' in df.columns else pd.Series('', index=df.index)
    k = df['Nº NF'].map(norm_nf_val) + '|' + cn.map(norm_cnpj_val)
    return df[~k.isin(chaves)].copy()


def _kpi(label: str, valor: str, nota: str = '', cor: str = 'var(--border)') -> str:
    nota_html = f'<div class="cb-kpi-nota">{nota}</div>' if nota else ''
    return (f'<div class="cb-kpi" style="--kpi-cor:{cor}">'
            f'<div class="cb-kpi-label" title="{html.escape(label)}">{html.escape(label)}</div>'
            f'<div class="cb-kpi-valor">{html.escape(valor)}</div>{nota_html}</div>')

def _fmt_int(n: int) -> str:
    return f"{n:,}".replace(',', '.')

def main():
    with st.spinner("Carregando a planilha…"):
        raw = buscar_planilha_bytes()
        df_merge, df_pend, df_fora, df_excl = processar(raw)
        df_nfs, cols_nfs, aba_nfs, sheet_names, erro_nfs = carregar_nfs(raw)
        df_controle = carregar_controle()
        st.session_state['atualizado_em'] = agora_br().strftime("%d/%m/%Y %H:%M")

    df_tratados = filtrar_tratados(df_merge, df_controle)
    df_pend_ativo = remover_tratados(df_pend, df_controle)
    df_merge_ativo = remover_tratados(df_merge, df_controle)

    # ── PageHeader: título + responsável (auditoria) + recarregar ──
    h_tit, h_resp, h_btn = st.columns([5, 3, 1.4])
    with h_tit:
        st.markdown(
            '<div class="cb-brand">Cobli</div>'
            '<h1 class="cb-title">Gestão de Manifestações NF-e</h1>'
            f'<div class="cb-sub">Cruzamento SAP × Qive × NFs · sincronizado em {st.session_state["atualizado_em"]}</div>',
            unsafe_allow_html=True,
        )
    with h_resp:
        responsavel = st.text_input("Responsável pela alteração", key="responsavel",
                                    placeholder="Seu nome (obrigatório para salvar)")
    with h_btn:
        st.markdown('<div style="height:28px"></div>', unsafe_allow_html=True)
        if st.button("Recarregar dados", use_container_width=True,
                     help="Limpa o cache e busca a planilha de novo"):
            st.cache_data.clear()
            st.rerun()

    # ── KPIs ──
    n_escopo = len(df_merge)
    n_pend   = len(df_pend_ativo)
    n_trat   = len(df_tratados)
    n_nfs    = int((df_merge.get('Na NFs', pd.Series(dtype=str)) == 'Sim').sum())
    valor_pend = sum(v for v in df_pend_ativo.get('Valor Nota Fiscal', pd.Series(dtype=str)).map(norm_valor)
                     if v is not None)
    pct_trat = f"{(n_trat / n_escopo * 100):.0f}%".replace('.', ',') if n_escopo else '—'
    st.markdown(
        '<div class="cb-kpis">'
        + _kpi("Pendências", _fmt_int(n_pend), "notas aguardando manifestação", "var(--warning)")
        + _kpi("Valor pendente", fmt_brl(valor_pend) or 'R$ 0,00', "soma das pendências", "var(--warning)")
        + _kpi("Tratados", _fmt_int(n_trat), f"<b>{pct_trat}</b> do escopo", "var(--success)")
        + _kpi("Escopo analítico", _fmt_int(n_escopo), f"{_fmt_int(n_nfs)} presentes na aba NFs", "var(--primary)")
        + _kpi("Descartados", _fmt_int(len(df_excl)), "devolução, retorno, comodato", "var(--ring)")
        + '</div>',
        unsafe_allow_html=True,
    )

    t1, t2, t_trat, t3, t4, t5 = st.tabs([
        f"Pendências ({len(df_pend_ativo)})",
        f"Notas SAP ({len(df_merge_ativo)})",
        f"Tratados ({len(df_tratados)})",
        f"Fora do escopo ({len(df_fora)})",
        f"Excluídos ({len(df_excl)})",
        "Detalhes da NF",
    ])
    with t1:
        render_editavel(df_pend_ativo, df_controle, df_nfs, cols_nfs, "pend", "pendencias", responsavel)
    with t2:
        render_editavel(df_merge_ativo, df_controle, df_nfs, cols_nfs, "merge", "cruzamento_sap_qive", responsavel)
    with t_trat:
        render_editavel(df_tratados, df_controle, df_nfs, cols_nfs, "tratados", "tratados", responsavel)
    with t3:
        st.caption("Notas que não são NF-e ou estão numa situação de manifestação fora da regra.")
        st.dataframe(df_fora, hide_index=True, use_container_width=True, height=420)
        btn_exportar(df_fora, "fora_do_escopo")
    with t4:
        st.caption("No escopo, mas descartadas pela regra de negócio (devolução, retorno, comodato ou CFOP de retorno).")
        st.dataframe(df_excl, hide_index=True, use_container_width=True, height=420)
        btn_exportar(df_excl, "excluidos")
    with t5:
        render_detalhes_nf(df_nfs, cols_nfs, aba_nfs, sheet_names, erro_nfs)

if __name__ == "__main__":
    main()
