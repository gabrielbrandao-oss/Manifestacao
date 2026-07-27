# app.py
import streamlit as st
import pandas as pd
import numpy as np
import io
import requests
import re
import gspread
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import Request
from datetime import datetime

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

st.set_page_config(page_title="Manifestações NF-e", layout="wide")
st.title("📋 Gestão de Manifestações NF-e")

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
        f"⬇️ Exportar {nome}",
        data=buf.getvalue(),
        file_name=f"{nome.replace(' ','_')}_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
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

@st.cache_data(show_spinner=False, ttl=30)
def carregar_controle() -> pd.DataFrame:
    df_vazio = pd.DataFrame(columns=['NF', 'CNPJ', 'Tratado', 'Observacao_Controle', 'Responsavel', 'Atualizado_em'])
    try:
        gc = gspread.authorize(get_credentials())
        sh = gc.open_by_key(get_sheet_id())
        try:
            ws = sh.worksheet(ABA_CONTROLE)
            dados = ws.get_all_records()
            df = pd.DataFrame(dados) if dados else df_vazio
            if 'NF' not in df.columns and 'Chave' in df.columns:
                df = df.rename(columns={'Chave': 'NF'})
            if 'CNPJ' not in df.columns:
                df['CNPJ'] = ''
            return df
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=ABA_CONTROLE, rows=5000, cols=6)
            ws.append_row(df_vazio.columns.tolist())
            return df_vazio
    except Exception as e:
        st.warning(f"gspread falhou: {e}")
        return df_vazio

def salvar_controle(df_controle: pd.DataFrame):
    try:
        gc = gspread.authorize(get_credentials())
        sh = gc.open_by_key(get_sheet_id())
        try:
            ws = sh.worksheet(ABA_CONTROLE)
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=ABA_CONTROLE, rows=5000, cols=6)
        ws.clear()
        df_salvar = df_controle.fillna('').astype(str)
        ws.update([df_salvar.columns.tolist()] + df_salvar.values.tolist())
        carregar_controle.clear()
        st.success("Controle salvo na planilha.")
    except Exception as e:
        st.error(f"Falha ao salvar controle: {e}")

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
    """Índices da aba NFs para checar presença por CNPJ, NF, valor e nome."""
    try:
        if df_nfs is None or df_nfs.empty:
            return None
        col_nf   = achar_coluna(df_nfs, COL_NF_NFS_CAND)
        col_cnpj = achar_coluna(df_nfs, COL_CNPJ_NFS_CAND)
        col_nome = achar_coluna(df_nfs, COL_NOME_NFS_CAND)
        col_va   = achar_coluna(df_nfs, ['Total da linha'])
        col_vb   = achar_coluna(df_nfs, ['Total documento'])
        set_nf   = set(df_nfs[col_nf].map(norm_nf_val))   if col_nf   else set()
        set_cnpj = set(df_nfs[col_cnpj].map(norm_cnpj_val)) if col_cnpj else set()
        nomes    = [_limpa_nome(x) for x in df_nfs[col_nome].astype(str)] if col_nome else []
        vals = set()
        for c in (col_va, col_vb):
            if c:
                vals |= {v for v in df_nfs[c].map(norm_valor) if v is not None}
        return {'nf': set_nf, 'cnpj': set_cnpj, 'nomes': list(set(n for n in nomes if n)), 'vals': vals}
    except Exception:
        return None

def _nome_parecido_de_lista(nome_limpo: str, nomes_limpos: list) -> bool:
    """Mesma regra de _nomes_parecidos, mas recebendo nomes JÁ limpos
    (evita rechamar _limpa_nome milhares de vezes por linha)."""
    if not nome_limpo:
        return True
    sa = set(nome_limpo.split())
    for nb in nomes_limpos:
        if not nb:
            continue
        if nome_limpo == nb or nome_limpo in nb or nb in nome_limpo:
            return True
        sb = set(nb.split())
        if sa and sb and len(sa & sb) / min(len(sa), len(sb)) >= 0.6:
            return True
    return False

def _presenca_nfs(df_nfs: pd.DataFrame, df_merge: pd.DataFrame) -> pd.Series:
    """Marca True quando a nota também aparece na aba NFs, batendo pelos campos
    disponíveis (CNPJ, Nº NF, valor e nome do fornecedor)."""
    idx = _nfs_index(df_nfs)
    if not idx:
        return pd.Series(False, index=df_merge.index)
    res = []
    nomes_nfs = idx['nomes']
    for _, r in df_merge.iterrows():
        nf   = norm_nf_val(r.get('Nº NF', ''))
        cn   = norm_cnpj_val(r.get('CNPJ Emitente', ''))
        val  = norm_valor(r.get('Valor Nota Fiscal', ''))
        nome = _limpa_nome(r.get('Razão Social', ''))
        # precisa identificar o fornecedor (CNPJ ou nome) E confirmar por NF ou valor
        forn_ok = (cn and cn in idx['cnpj']) or _nome_parecido_de_lista(nome, nomes_nfs)
        doc_ok  = (nf and nf in idx['nf']) or (val is not None and val in idx['vals'])
        res.append(bool(forn_ok and doc_ok))
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
    # CFOP contendo o dígito 9 (ex.: 5909, 6949, 6919...) sai do escopo
    col_cfop = achar_coluna(df_no_escopo, COL_CFOP_CAND)
    if col_cfop is not None:
        cfop_tem9 = df_no_escopo[col_cfop].astype(str).str.contains('9', na=False)
        mask_excl |= cfop_tem9
    df_excluidos = df_no_escopo[mask_excl].copy()
    df_ativo     = df_no_escopo[~mask_excl].copy()

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

def descricao_da_nf(df_nfs, cols_nfs, nf, cnpj, nome_nota, valor_nota) -> str:
    """Traz a descrição do item/serviço (coluna E da aba NF's) — o campo que explica
    do que se trata a nota.
    1) Casamento EXATO por Nº NF + CNPJ Emitente (mesma chave usada no cruzamento
       SAP × NFs — é o jeito mais confiável de achar a nota certa).
    2) Se não achar nada por aí, cai para o casamento por NOME do fornecedor + VALOR
       exato (fallback antigo, útil quando a nota não tem NF/CNPJ correspondente)."""
    col_desc = cols_nfs.get('col_desc')
    if df_nfs is None or df_nfs.empty or col_desc is None or col_desc not in df_nfs.columns:
        return ""

    def _unicas(serie: pd.Series) -> str:
        descricoes = [d.strip() for d in serie.astype(str).tolist()
                      if d.strip() and d.strip().lower() not in LIXO]
        vistos, out = set(), []
        for d in descricoes:
            if d not in vistos:
                vistos.add(d); out.append(d)
        return " | ".join(out)

    # 1) Match exato por Nº NF + CNPJ Emitente
    col_nf, col_cnpj = cols_nfs.get('col_nf'), cols_nfs.get('col_cnpj')
    if col_nf is not None and nf:
        cand = df_nfs[df_nfs[col_nf].map(norm_nf_val) == norm_nf_val(nf)]
        if col_cnpj is not None and cnpj:
            cand_cnpj = cand[cand[col_cnpj].map(norm_cnpj_val) == norm_cnpj_val(cnpj)]
            if not cand_cnpj.empty:
                cand = cand_cnpj
        if not cand.empty:
            desc = _unicas(cand[col_desc])
            if desc:
                return desc

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

    # "Do que se trata" — mesma lógica do painel de detalhes, calculada linha a linha
    if df_nfs is not None and cols_nfs is not None and 'Nº NF' in df.columns:
        def _desc_linha(row):
            return descricao_da_nf(
                df_nfs, cols_nfs,
                row.get('Nº NF', ''), row.get('CNPJ Emitente', ''),
                row.get('Razão Social', ''), row.get('Valor Nota Fiscal', ''),
            )
        df_view.insert(
            min(5, len(df_view.columns)), '📖 Do que se trata',
            df.apply(_desc_linha, axis=1)
        )

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
                              placeholder="Buscar por Nº NF, CNPJ, emitente ou chave…",
                              label_visibility="collapsed")
    with col_status:
        opts = ["Todos"]
        if 'Situação Manifestação' in df.columns:
            opts += sorted(df['Situação Manifestação'].dropna().unique().tolist())
        sel = st.selectbox("status", opts, key=f"status_{key}", label_visibility="collapsed")

    cc_sel = "Todos"
    if tem_cc:
        todos_cc = set()
        for v in df['Centro de Custo'].dropna().astype(str):
            for parte in v.split('|'):
                p = parte.strip()
                if p:
                    todos_cc.add(p)
        with col_cc:
            cc_sel = st.selectbox("centro de custo", ["Todos"] + sorted(todos_cc),
                                  key=f"cc_{key}", label_visibility="collapsed")

    tres_sel = "Todos"
    if tem_3:
        with col_3:
            tres_sel = st.selectbox("nas 3 abas",
                                    ["Todos", "Só nas 3 abas", "Falta em alguma"],
                                    key=f"tres_{key}", label_visibility="collapsed")

    with col_reset:
        if st.button("↺", key=f"reset_{key}"):
            for k in [f"busca_{key}", f"status_{key}", f"cc_{key}", f"tres_{key}"]:
                st.session_state.pop(k, None)
            st.rerun()

    res = df.copy()
    if busca:
        mask = pd.Series(False, index=res.index)
        for col in ['Nº NF', 'CNPJ Emitente', 'Chave de Acesso', 'Razão Social']:
            if col in res.columns:
                mask |= res[col].astype(str).str.contains(busca, case=False, na=False)
        res = res[mask]
    if sel != "Todos" and 'Situação Manifestação' in res.columns:
        res = res[res['Situação Manifestação'] == sel]
    if cc_sel != "Todos" and 'Centro de Custo' in res.columns:
        res = res[res['Centro de Custo'].astype(str).str.contains(rf'(^|\|)\s*{re.escape(cc_sel)}\s*($|\|)', regex=True)]
    if tres_sel != "Todos" and 'Nas 3 abas' in res.columns:
        nas3 = res['Nas 3 abas'].astype(str).str.startswith('✅')
        res = res[nas3] if tres_sel == "Só nas 3 abas" else res[~nas3]
    return res

def painel_detalhes(registro: pd.Series, nf, cnpj, df_nfs, cols_nfs):
    st.markdown("#### 📑 Dados da nota selecionada")
    campos = ['Nº NF', 'CNPJ Emitente', 'Razão Social', 'Valor Nota Fiscal',
              'Situação Manifestação']
    kv = []
    for c in campos:
        if c in registro.index:
            val = str(registro[c]).strip()
            if val and val.lower() not in LIXO:
                kv.append({'Campo': c, 'Valor': val})

    # "Do que se trata" — Descrição do item/serviço (coluna E da aba NF's) —
    # entra como mais uma linha da MESMA tabela, junto com os dados da nota.
    nome_nota  = str(registro.get('Razão Social', ''))
    valor_nota = registro.get('Valor Nota Fiscal', '')
    desc = descricao_da_nf(df_nfs, cols_nfs, nf, cnpj, nome_nota, valor_nota)
    kv.append({
        'Campo': '📖 Do que se trata',
        'Valor': desc if desc else "Não encontrada na aba NF's."
    })

    if kv:
        st.dataframe(pd.DataFrame(kv), hide_index=True, use_container_width=True)

def render_editavel(df_dados: pd.DataFrame, df_controle: pd.DataFrame, df_nfs, cols_nfs,
                    key: str, nome_export: str, responsavel: str):
    df_filtrado = filtros(df_dados, key).reset_index(drop=True)
    st.caption(f"{len(df_filtrado)} registro(s) — clique numa linha para abrir os detalhes abaixo")

    df_view = montar_tabela_editavel(df_filtrado, df_controle, df_nfs, cols_nfs).reset_index(drop=True)

    event = st.dataframe(
        df_view,
        column_config={
            'Tratado': st.column_config.CheckboxColumn("✅ Tratado"),
            'Observacao_Controle': st.column_config.TextColumn("📝 Observação"),
        },
        hide_index=True, use_container_width=True,
        on_select="rerun", selection_mode="single-row",
        key=f"sel_{key}",
    )

    # Linha selecionada
    try:
        rows = event.selection["rows"]
    except Exception:
        rows = getattr(getattr(event, "selection", None), "rows", []) or []
    sel_idx = rows[0] if rows and rows[0] < len(df_view) else None
    linha = df_view.iloc[sel_idx] if sel_idx is not None else None
    nf    = norm_nf_val(linha.get('Nº NF', '')) if linha is not None else ''
    cnpj  = norm_cnpj_val(linha.get('CNPJ Emitente', '')) if linha is not None else ''

    # ── BARRA SUPERIOR: Exportar + Controle desta nota (lado a lado) ──
    b_exp, b_chk, b_obs, b_save = st.columns([2, 1, 3, 1])
    with b_exp:
        btn_exportar(df_filtrado, nome_export)
    with b_chk:
        novo_tratado = st.checkbox("✅ Tratado",
                                   value=bool(linha.get('Tratado', False)) if linha is not None else False,
                                   key=f"chk_{key}", disabled=(linha is None))
    with b_obs:
        nova_obs = st.text_input("📝 Observação",
                                 value=str(linha.get('Observacao_Controle', '') or '') if linha is not None else '',
                                 key=f"obs_{key}", disabled=(linha is None),
                                 label_visibility="collapsed", placeholder="Observação desta nota…")
    with b_save:
        salvar_click = st.button("💾 Salvar", type="primary", key=f"savrow_{key}", disabled=(linha is None))

    if linha is None:
        st.info("👆 Selecione uma linha acima para ver os detalhes e habilitar o controle.")
        return

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
        if not responsavel:
            st.error("Preencha o responsável na barra lateral.")
            st.stop()
        if not nf:
            st.error("Esta linha não tem Nº NF.")
            st.stop()
        agora = datetime.now().strftime("%d/%m/%Y %H:%M")
        nova_linha = pd.DataFrame([{
            'NF': nf, 'CNPJ': cnpj, 'Tratado': novo_tratado,
            'Observacao_Controle': nova_obs, 'Responsavel': responsavel, 'Atualizado_em': agora,
        }])
        if df_controle is not None and not df_controle.empty and {'NF', 'CNPJ'}.issubset(df_controle.columns):
            k_atual = nf + '|' + cnpj
            kctrl = df_controle['NF'].map(norm_nf_val) + '|' + df_controle['CNPJ'].map(norm_cnpj_val)
            restante = df_controle[kctrl != k_atual]
        else:
            restante = pd.DataFrame(columns=['NF', 'CNPJ', 'Tratado', 'Observacao_Controle', 'Responsavel', 'Atualizado_em'])
        novo_ctrl = pd.concat([restante, nova_linha], ignore_index=True)
        salvar_controle(novo_ctrl)
        buscar_planilha_bytes.clear()
        st.rerun()

    st.divider()
    painel_detalhes(registro, nf, cnpj, df_nfs, cols_nfs)

def render_detalhes_nf(df_nfs, cols_nfs, aba_real, sheet_names, erro):
    col_nf   = cols_nfs.get('col_nf')
    col_cnpj = cols_nfs.get('col_cnpj')
    st.subheader("🔎 Detalhes por Nota Fiscal (busca global)")
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

def main():
    with st.sidebar:
        st.header("Auditoria")
        responsavel = st.text_input("👤 Responsável pela alteração", key="responsavel")
        st.divider()
        if st.button("🔄 Forçar Recarga de Cache", type="primary", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
        if 'atualizado_em' in st.session_state:
            st.caption(f"Sincronizado: {st.session_state['atualizado_em']}")

    with st.spinner("Processando..."):
        raw = buscar_planilha_bytes()
        df_merge, df_pend, df_fora, df_excl = processar(raw)
        df_nfs, cols_nfs, aba_nfs, sheet_names, erro_nfs = carregar_nfs(raw)
        df_controle = carregar_controle()
        st.session_state['atualizado_em'] = datetime.now().strftime("%d/%m/%Y %H:%M")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Escopo Analítico", len(df_merge))
    c2.metric("Pendências", len(df_pend))
    c3.metric("Presentes na aba NFs", int((df_merge.get('Na NFs', pd.Series(dtype=str)) == 'Sim').sum()))
    c4.metric("Descartados (Lógica de Negócio)", len(df_excl))

    t1, t2, t3, t4, t5 = st.tabs([
        f"⚠️ Pendências ({len(df_pend)})",
        f"📄 Notas SAP ({len(df_merge)})",
        f"📂 Fora do Escopo ({len(df_fora)})",
        f"📦 Excluídos ({len(df_excl)})",
        "🔎 Detalhes da NF",
    ])
    with t1:
        render_editavel(df_pend, df_controle, df_nfs, cols_nfs, "pend", "pendencias", responsavel)
    with t2:
        render_editavel(df_merge, df_controle, df_nfs, cols_nfs, "merge", "cruzamento_sap_qive", responsavel)
    with t3:
        st.dataframe(df_fora, hide_index=True, use_container_width=True)
        btn_exportar(df_fora, "fora_do_escopo")
    with t4:
        st.dataframe(df_excl, hide_index=True, use_container_width=True)
        btn_exportar(df_excl, "excluidos")
    with t5:
        render_detalhes_nf(df_nfs, cols_nfs, aba_nfs, sheet_names, erro_nfs)

if __name__ == "__main__":
    main()