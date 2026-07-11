#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Monitor de leilões de livros — LeilõesBR (leiloesbr.com.br)
"""

import argparse
import csv
import os
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

BASE = "https://www.leiloesbr.com.br"
CAT_LIVROS_HEX = "4C6976726F73"
URL_ANDAMENTO = (
    BASE + "/busca_andamento.asp?pesquisa=&op=2&v=126&tp=|{cat}|&b=0&pag={pag}"
)
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leiloes.db")
CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leiloes.csv")
AUTORES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "autores.txt")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; monitor-pessoal-livros/1.0)",
    "Accept-Language": "pt-BR,pt;q=0.9",
}
PAUSA_ENTRE_PAGINAS = 3
MAX_PAGINAS = 60

ANO_LIMITE = 1940

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

LEILOESBR_EMAIL = os.environ.get("LEILOESBR_EMAIL", "")
LEILOESBR_SENHA = os.environ.get("LEILOESBR_SENHA", "")

COLECAO_CSV_URL = os.environ.get("COLECAO_CSV_URL", "")

VARIANTES = {
    "Manuel Antônio de Almeida": ["Manoel Antonio de Almeida"],
    "Cláudio Manoel da Costa": ["Claudio Manuel da Costa"],
    "Raimundo Correia": ["Raimundo Correa", "Raymundo Correa"],
    "Araújo Porto-Alegre": ["Araujo Porto Alegre", "Porto-alegre"],
    "José Bonifácio": ["Jose Bonifacio o Moço", "Jose Bonifacio o Moco"],
    "Visconde de Taunay": ["Alfredo d'Escragnolle Taunay", "Alfredo Taunay"],
    "Gregório de Matos": ["Gregorio de Mattos"],
    "Martins Pena": ["Martins Penna"],
    "Julia Lopes de Almeida": ["Júlia Lopes de Almeida"],
}


def normalizar(texto: str) -> str:
    t = unicodedata.normalize("NFKD", texto)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower().replace("y", "i").replace("z", "s")
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def gerar_variantes_reversas(nome_normalizado: str) -> set:
    palavras = nome_normalizado.split()
    variantes = set()
    for k in (1, 2):
        if len(palavras) > k:
            sobrenome = " ".join(palavras[-k:])
            resto = " ".join(palavras[:-k])
            if resto:
                variantes.add(f"{sobrenome} {resto}")
    return variantes


def carregar_autores(caminho: str):
    autores = []
    with open(caminho, encoding="utf-8") as f:
        nomes = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    for nome in nomes:
        padroes = {normalizar(nome)}
        for var in VARIANTES.get(nome, []):
            padroes.add(normalizar(var))
        reversas = set()
        for p in padroes:
            reversas |= gerar_variantes_reversas(p)
        padroes |= reversas
        autores.append((nome, sorted(padroes)))
    return autores


def casar_autores(texto: str, autores) -> list:
    t = " " + normalizar(texto) + " "
    encontrados = []
    for nome, padroes in autores:
        if any((" " + p + " ") in t for p in padroes):
            encontrados.append(nome)
    return encontrados


RE_ANO = re.compile(r"\b(1[5-9]\d{2}|20[0-2]\d)\b")


def extrair_anos(texto: str) -> list:
    return [int(a) for a in RE_ANO.findall(texto)]


def classificar_epoca(descricao: str):
    anos = extrair_anos(descricao)
    if not anos:
        return True, "indefinido"
    mais_recente = max(anos)
    passa = mais_recente < ANO_LIMITE
    return passa, ", ".join(str(a) for a in sorted(set(anos)))


def extrair_titulo(descricao: str) -> str:
    txt = descricao.strip()

    m = re.search(r'["“]([^"”]{3,100})["”]', txt)
    if m:
        return m.group(1).strip()

    m = re.match(r'^[A-ZÀ-Ú][A-ZÀ-Úa-zà-ú\.,\s]{1,45}?[-–]\s*(.+?)\.', txt)
    if m:
        candidato = m.group(1).strip(" .,-–")
        if 3 <= len(candidato) <= 120:
            return candidato

    m = re.match(r'^[^,]{3,40},\s*(.+?),\s*\d{4}', txt)
    if m:
        candidato = m.group(1).strip(" .,-–")
        if 3 <= len(candidato) <= 120:
            return candidato

    return ""


def extrair_info_adicional(descricao: str, titulo: str) -> str:
    txt = descricao.strip()
    if not titulo:
        return txt
    idx = txt.find(titulo)
    if idx == -1:
        return txt
    resto = txt[:idx] + txt[idx + len(titulo):]
    resto = re.sub(r'^[\s\.,\-–"“”]+', '', resto)
    resto = re.sub(r'[-–]\s*\.', '.', resto)
    resto = re.sub(r'\s+', ' ', resto).strip()
    return resto


STOPWORDS_TITULO = {
    "de", "da", "do", "das", "dos", "e", "a", "o", "as", "os", "em", "um",
    "uma", "para", "com", "por", "no", "na", "nos", "nas", "obras",
}


def similaridade_titulos(a: str, b: str) -> float:
    wa = {w for w in normalizar(a).split() if len(w) > 2 and w not in STOPWORDS_TITULO}
    wb = {w for w in normalizar(b).split() if len(w) > 2 and w not in STOPWORDS_TITULO}
    if not wa or not wb:
        return 0.0
    inter = wa & wb
    menor = min(len(wa), len(wb))
    return len(inter) / menor


def para_ano(valor):
    try:
        return int(float(valor))
    except (ValueError, TypeError):
        return None


def carregar_colecao(url: str) -> dict:
    if not url:
        return {}
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"[aviso] não consegui baixar a planilha de coleção: {e}", file=sys.stderr)
        return {}

    texto = r.content.decode("utf-8-sig", errors="replace")
    leitor = csv.reader(texto.splitlines())
    linhas = list(leitor)
    if not linhas:
        return {}
    cabecalho = [normalizar(c) for c in linhas[0]]

    def idx(*nomes_possiveis):
        for nome in nomes_possiveis:
            if nome in cabecalho:
                return cabecalho.index(nome)
        return None

    i_autor = idx("autores", "autor")
    i_titulo = idx("titulo")
    i_tenho = idx("tenho")
    i_ano = idx("ano")
    i_valor = idx("valor pago", "valor")
    i_data = idx("data de aquisicao", "data aquisicao", "data")
    i_comentario = idx("comentario", "comentarios")

    if i_autor is None or i_titulo is None:
        print("[aviso] planilha de coleção sem colunas AUTORES/TITULO "
              "reconhecíveis — comparação desativada nesta execução.",
              file=sys.stderr)
        return {}

    colecao = {}
    for linha in linhas[1:]:
        if len(linha) <= max(i_autor, i_titulo):
            continue
        autor = linha[i_autor].strip()
        titulo = linha[i_titulo].strip()
        if not autor or not titulo:
            continue
        registro = {
            "titulo": titulo,
            "tenho": (linha[i_tenho].strip().upper() == "TENHO"
                      if i_tenho is not None and i_tenho < len(linha) else False),
            "ano": para_ano(linha[i_ano]) if i_ano is not None and i_ano < len(linha) else None,
            "valor_pago": linha[i_valor] if i_valor is not None and i_valor < len(linha) else "",
            "data_aquisicao": linha[i_data] if i_data is not None and i_data < len(linha) else "",
            "comentario": linha[i_comentario] if i_comentario is not None and i_comentario < len(linha) else "",
        }
        colecao.setdefault(normalizar(autor), []).append(registro)
    return colecao


LIMIAR_SIMILARIDADE = 0.6


def avaliar_contra_colecao(autor: str, titulo_leilao: str, ano_leilao, colecao: dict):
    if not colecao:
        return "sem_colecao", None
    registros = colecao.get(normalizar(autor), [])
    if not registros:
        return "desconhecido_novo", None

    melhor, melhor_score = None, 0.0
    for reg in registros:
        s = similaridade_titulos(titulo_leilao or "", reg["titulo"])
        if s > melhor_score:
            melhor_score, melhor = s, reg
    if melhor_score < LIMIAR_SIMILARIDADE:
        return "desconhecido_novo", None
    if melhor["tenho"]:
        if para_ano(ano_leilao) == melhor["ano"]:
            return "ja_tenho_mesma_edicao", melhor
        return "ja_tenho_outra_edicao", melhor
    return "falta_edicao_conhecida", melhor


def buscar_pagina(sessao: requests.Session, pag: int) -> str:
    url = URL_ANDAMENTO.format(cat=CAT_LIVROS_HEX, pag=pag)
    r = sessao.get(url, headers=HEADERS, timeout=40)
    r.raise_for_status()
    return r.text


RE_PRECO = re.compile(r"R\$\s*([\d\.]+,\d{2})")
RE_DATA = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})\s*-\s*([\d:]+h?)[^A-Z]*([A-Z]{2})?")
RE_LOTE = re.compile(r"abre_catalogo\.asp\?t=\d+\|([^|]+)\|(\d+)\|(\d+)")


def extrair_lotes(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    lotes = {}
    for a in soup.find_all("a", href=RE_LOTE):
        m = RE_LOTE.search(a.get("href", ""))
        if not m:
            continue
        site_leiloeiro, id_leilao, id_lote = m.group(1), m.group(2), m.group(3)
        chave = f"{id_leilao}-{id_lote}"
        titulo = (a.get("title") or a.get_text(" ", strip=True) or "").strip()
        if chave in lotes and len(titulo) <= len(lotes[chave]["descricao"]):
            continue
        contexto = a
        preco = data_str = uf = galeria = ""
        texto_mais_amplo = ""
        for _ in range(6):
            contexto = contexto.parent
            if contexto is None:
                break
            txt = contexto.get_text(" ", strip=True)
            texto_mais_amplo = txt
            if not preco:
                mp = RE_PRECO.search(txt)
                if mp:
                    preco = mp.group(1)
            if not data_str:
                md = RE_DATA.search(txt)
                if md:
                    data_str = md.group(1) + " " + (md.group(2) or "")
                    uf = md.group(3) or ""
            if not galeria:
                for g in contexto.find_all("a", href=re.compile(r"^https?://")):
                    if "leiloesbr" not in g.get("href", ""):
                        galeria = g.get_text(strip=True)
                        break
            if preco and data_str and galeria:
                break

        baixo = texto_mais_amplo.lower()
        if "não vendido" in baixo or "nao vendido" in baixo or "sem lance" in baixo:
            status_venda = "nao_vendido"
        elif preco:
            status_venda = "vendido"
        else:
            status_venda = None

        lotes[chave] = {
            "id": chave,
            "descricao": titulo,
            "preco_inicial": preco,
            "status_venda": status_venda,
            "data_pregao": data_str.strip(),
            "uf": uf,
            "leiloeiro": galeria,
            "url": f"{BASE}/abre_catalogo.asp?t=1|{site_leiloeiro}|{id_leilao}|{id_lote}",
        }
    return list(lotes.values())


def raspar_andamento(sessao: requests.Session) -> list:
    todos, vistos = [], set()
    for pag in range(1, MAX_PAGINAS + 1):
        try:
            html = buscar_pagina(sessao, pag)
        except requests.RequestException as e:
            print(f"[aviso] falha na página {pag}: {e}", file=sys.stderr)
            break
        lotes = extrair_lotes(html)
        novos = [l for l in lotes if l["id"] not in vistos]
        if not novos:
            break
        for l in novos:
            vistos.add(l["id"])
        todos.extend(novos)
        print(f"  página {pag}: {len(novos)} lotes")
        time.sleep(PAUSA_ENTRE_PAGINAS)
    return todos


def buscar_valor_final(sessao: requests.Session, url_lote: str):
    try:
        r = sessao.get(url_lote, headers=HEADERS, timeout=40)
        r.raise_for_status()
    except requests.RequestException:
        return None, None
    txt = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
    baixo = txt.lower()
    m = RE_PRECO.search(txt)
    valor = m.group(1) if m else None
    if any(p in baixo for p in ("arrematado", "vendido", "lote vendido")):
        return valor, "vendido"
    if any(p in baixo for p in ("não vendido", "nao vendido", "sem lance", "retirado")):
        return valor, "nao_vendido"
    if any(p in baixo for p in ("encerrado", "finalizado")):
        return valor, "encerrado"
    return None, None


def buscar_pagina_renderizada(navegador, url: str, timeout_ms: int = 40000) -> str:
    """
    Abre a URL num navegador headless (Chromium via Playwright) e devolve
    o HTML já processado pelo JavaScript da página.

    Necessário porque descobrimos que o valor "Vendido por: R$ X" nas
    buscas de leilões finalizados só aparece depois que o JavaScript da
    própria página carrega essa informação — uma busca simples (sem
    navegador) nunca vê esse valor, mesmo logado.
    """
    pagina = navegador.new_page()
    try:
        pagina.goto(url, timeout=timeout_ms, wait_until="networkidle")
        pagina.wait_for_timeout(800)
        return pagina.content()
    except Exception as e:
        print(f"  [aviso] falha ao renderizar página: {e}", file=sys.stderr)
        return ""
    finally:
        pagina.close()


def autenticar(sessao: requests.Session) -> bool:
    if not (LEILOESBR_EMAIL and LEILOESBR_SENHA):
        return False
    login_url = f"{BASE}/login_site.asp"
    try:
        sessao.get(login_url, headers=HEADERS, timeout=30)
    except requests.RequestException as e:
        print(f"[aviso] não consegui abrir a página de login: {e}", file=sys.stderr)
        return False

    tentativas = [
        {"Email": LEILOESBR_EMAIL, "Senha": LEILOESBR_SENHA},
        {"email": LEILOESBR_EMAIL, "senha": LEILOESBR_SENHA},
        {"txtEmail": LEILOESBR_EMAIL, "txtSenha": LEILOESBR_SENHA},
        {"login": LEILOESBR_EMAIL, "senha": LEILOESBR_SENHA},
        {"Email": LEILOESBR_EMAIL, "Password": LEILOESBR_SENHA},
    ]
    for campos in tentativas:
        try:
            r = sessao.post(login_url, data=campos, headers=HEADERS,
                             timeout=30, allow_redirects=True)
        except requests.RequestException:
            continue
        texto = r.text.lower()
        indicios_logado = ("minha conta" in texto and "faça seu login" not in texto) \
            or "sair da conta" in texto or "logout" in texto
        if indicios_logado:
            return True
    print("[aviso] não consegui confirmar login automático — "
          "os campos do formulário podem ter nomes diferentes do esperado.",
          file=sys.stderr)
    return False


def abrir_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS lotes (
            id            TEXT PRIMARY KEY,
            autor         TEXT,
            titulo        TEXT,
            ano           INTEGER,
            descricao     TEXT,
            comentarios   TEXT,
            status_colecao TEXT,
            leiloeiro     TEXT,
            uf            TEXT,
            data_pregao   TEXT,
            preco_inicial TEXT,
            valor_final   TEXT,
            status        TEXT DEFAULT 'em_andamento',
            url           TEXT,
            visto_em      TEXT,
            atualizado_em TEXT
        )""")

    colunas = {row[1] for row in con.execute("PRAGMA table_info(lotes)")}

    if "autor" not in colunas:
        if "autores" in colunas:
            try:
                con.execute("ALTER TABLE lotes RENAME COLUMN autores TO autor")
            except sqlite3.OperationalError:
                con.execute("ALTER TABLE lotes ADD COLUMN autor TEXT")
                con.execute("UPDATE lotes SET autor = autores")
        else:
            con.execute("ALTER TABLE lotes ADD COLUMN autor TEXT")

    for coluna, tipo in (("titulo", "TEXT"), ("ano", "INTEGER"),
                         ("comentarios", "TEXT"), ("status_colecao", "TEXT")):
        if coluna not in colunas:
            con.execute(f"ALTER TABLE lotes ADD COLUMN {coluna} {tipo}")
    con.commit()

    if "anos_detectados" in colunas:
        pendentes = con.execute(
            "SELECT id, descricao, anos_detectados FROM lotes "
            "WHERE titulo IS NULL OR titulo=''").fetchall()
        for id_, descricao, anos_str in pendentes:
            titulo = extrair_titulo(descricao or "")
            comentarios = extrair_info_adicional(descricao or "", titulo)
            ano = None
            if anos_str and anos_str != "indefinido":
                try:
                    ano = max(int(a.strip()) for a in anos_str.split(","))
                except ValueError:
                    ano = None
            con.execute("UPDATE lotes SET titulo=?, ano=?, comentarios=? WHERE id=?",
                        (titulo, ano, comentarios, id_))
        con.commit()
        if pendentes:
            print(f"Migração: {len(pendentes)} lote(s) antigos atualizados "
                  f"com título/ano.")
    return con


def exportar_csv(con):
    cur = con.execute("""SELECT autor, titulo, ano, status_colecao, descricao,
                                comentarios, leiloeiro, uf, data_pregao,
                                preco_inicial, valor_final, status,
                                url, visto_em
                         FROM lotes ORDER BY visto_em DESC""")
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Autor", "Título", "Ano", "Situação na coleção",
                    "Descrição completa", "Comentários", "Leiloeiro", "UF",
                    "Data do pregão", "Lance inicial (R$)", "Valor final (R$)",
                    "Status", "Link", "Detectado em"])
        w.writerows(cur.fetchall())
    print(f"Planilha atualizada: {CSV_PATH}")


def rodar_backfill(autores, sessao, con, logado, colecao, dry_run=False):
    agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    total_novos = 0
    total_pesquisados = 0
    stats = {}

    with sync_playwright() as p:
        navegador = p.chromium.launch()
        try:
            for nome, _ in autores:
                total_pesquisados += 1
                print(f"[{total_pesquisados}/{len(autores)}] Buscando: {nome}", flush=True)
                pag = 1
                vistos_ids = set()
                while pag <= MAX_PAGINAS:
                    url = (f"{BASE}/busca_finalizado.asp?pesquisa={quote(nome)}"
                           f"&tp=|&op=2&v=126&pag={pag}")
                    html = buscar_pagina_renderizada(navegador, url)
                    time.sleep(PAUSA_ENTRE_PAGINAS)
                    if not html:
                        break
                    lotes = extrair_lotes(html)
                    lotes_novos_pagina = [l for l in lotes if l["id"] not in vistos_ids]
                    if not lotes_novos_pagina:
                        break
                    for l in lotes_novos_pagina:
                        vistos_ids.add(l["id"])
                    print(f"  página {pag}: {len(lotes_novos_pagina)} lotes", flush=True)
                    for lote in lotes_novos_pagina:
                        achados = casar_autores(lote["descricao"], autores)
                        if not achados:
                            continue
                        passa_epoca, anos_str = classificar_epoca(lote["descricao"])
                        if not passa_epoca:
                            continue
                        if con.execute("SELECT 1 FROM lotes WHERE id=?",
                                        (lote["id"],)).fetchone():
                            continue

                        valor_final = lote["preco_inicial"] or None
                        status = lote["status_venda"] or "encerrado_historico"
                        ano = (max(int(a) for a in anos_str.split(", "))
                               if anos_str != "indefinido" else None)
                        titulo = extrair_titulo(lote["descricao"])
                        texto_comparar = titulo or lote["descricao"]
                        status_col, reg_col = avaliar_contra_colecao(achados[0], texto_comparar, ano, colecao)

                        con.execute(
                            """INSERT INTO lotes (id, autor, titulo, ano, descricao,
                                                  comentarios, status_colecao,
                                                  leiloeiro, uf, data_pregao,
                                                  preco_inicial, valor_final, status, url,
                                                  visto_em, atualizado_em)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (lote["id"], ", ".join(achados), titulo,
                             ano, lote["descricao"],
                             extrair_info_adicional(lote["descricao"], titulo), status_col,
                             lote["leiloeiro"], lote["uf"],
                             lote["data_pregao"], lote["preco_inicial"], valor_final,
                             status, lote["url"], agora, agora))
                        total_novos += 1
                        stats[status_col] = stats.get(status_col, 0) + 1

                        if status_col == "falta_edicao_conhecida":
                            valor_mostrado = valor_final or "não informado"
                            enviar_telegram(
                                "🔴 FALTA NA COLEÇÃO\n"
                                f"Autor: {', '.join(achados)}\n"
                                f"Título: {titulo or lote['descricao'][:150]}\n"
                                f"Ano: {ano if ano else 'não identificado'}\n"
                                f"Valor: R$ {valor_mostrado}\n"
                                f"Pregão: {lote['data_pregao'] or '?'}\n"
                                f"{lote['url']}",
                                dry_run=dry_run)
                    con.commit()
                    pag += 1
        finally:
            navegador.close()

    print(f"\nBackfill concluído. Autores pesquisados: {total_pesquisados}. "
          f"Novos lotes adicionados ao histórico: {total_novos}.")
    print("Por situação na coleção:", stats)

    resumo = (
        f"📚 Backfill concluído!\n"
        f"{total_novos} lotes novos adicionados ao histórico "
        f"({total_pesquisados} autores pesquisados).\n\n"
        f"🚨 Não constavam na sua lista: {stats.get('desconhecido_novo', 0)}\n"
        f"🔴 Faltam na coleção: {stats.get('falta_edicao_conhecida', 0)}\n"
        f"🟡 Já tem, outra edição: {stats.get('ja_tenho_outra_edicao', 0)}\n"
        f"✅ Já tem, mesma edição: {stats.get('ja_tenho_mesma_edicao', 0)}\n\n"
        f"Confira os detalhes na planilha (leiloes.csv)."
    )
    enviar_telegram(resumo, dry_run=dry_run)


def rotulo_status_colecao(status: str, registro):
    if status == "desconhecido_novo":
        return ("🚨 NÃO CONSTA NA SUA LISTA — você não sabia que este "
                "título existia!")
    if status == "falta_edicao_conhecida":
        extra = f" (edição de {registro['ano']})" if registro and registro.get("ano") else ""
        return f"🔴 FALTA NA COLEÇÃO{extra} — você já conhecia, mas ainda não tem"
    if status == "ja_tenho_outra_edicao":
        ano_atual = registro["ano"] if registro else "?"
        valor = registro.get("valor_pago") if registro else ""
        extra = f" (comprada por R$ {valor})" if valor else ""
        return (f"🟡 Você já tem uma edição de {ano_atual}{extra} — "
                f"esta é de ano diferente, avalie se vale a pena")
    if status == "ja_tenho_mesma_edicao":
        return "✅ Você já tem exatamente esta edição"
    return ""


def deve_alertar_colecao(status: str) -> bool:
    return status != "ja_tenho_mesma_edicao"


def enviar_telegram(msg: str, dry_run: bool = False):
    if dry_run or not (TELEGRAM_TOKEN and TELEGRAM_CHAT):
        print("[telegram desativado] " + msg.replace("\n", " | "))
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": msg,
                  "disable_web_page_preview": True},
            timeout=30,
        )
    except requests.RequestException as e:
        print(f"[aviso] falha no Telegram: {e}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--backfill", action="store_true")
    args = ap.parse_args()

    autores = carregar_autores(AUTORES_PATH)
    print(f"Monitorando {len(autores)} autores.")
    con = abrir_db()
    sessao = requests.Session()

    colecao = carregar_colecao(COLECAO_CSV_URL)
    if COLECAO_CSV_URL:
        print(f"Coleção carregada: {len(colecao)} autores com referência "
              f"({'OK' if colecao else 'FALHOU — confira o link'}).")
    else:
        print("Comparação com a coleção não configurada (COLECAO_CSV_URL vazio).")

    logado = autenticar(sessao)
    if LEILOESBR_EMAIL:
        print(f"Login no LeilõesBR: {'OK' if logado else 'FALHOU'}")
    else:
        print("Login não configurado — valores finais de leilões "
              "encerrados não serão capturados.")

    if args.backfill:
        rodar_backfill(autores, sessao, con, logado, colecao, dry_run=args.dry_run)
        exportar_csv(con)
        con.close()
        print("Concluído.")
        return

    agora = datetime.now(timezone.utc).isoformat(timespec="seconds")

    print("Raspando categoria Livros (em andamento)...")
    lotes = raspar_andamento(sessao)
    print(f"Total de lotes na categoria: {len(lotes)}")

    novos_alertas = 0
    descartados_epoca = 0
    colecao_stats = {}
    for lote in lotes:
        achados = casar_autores(lote["descricao"], autores)
        if not achados:
            continue
        passa_epoca, anos_str = classificar_epoca(lote["descricao"])
        if not passa_epoca:
            descartados_epoca += 1
            continue
        ja_existe = con.execute(
            "SELECT 1 FROM lotes WHERE id=?", (lote["id"],)).fetchone()
        if ja_existe:
            con.execute("UPDATE lotes SET atualizado_em=? WHERE id=?",
                        (agora, lote["id"]))
            continue

        ano = (max(int(a) for a in anos_str.split(", ")) if anos_str != "indefinido" else None)
        titulo = extrair_titulo(lote["descricao"])
        texto_comparar = titulo or lote["descricao"]
        status_col, reg_col = avaliar_contra_colecao(achados[0], texto_comparar, ano, colecao)

        con.execute(
            """INSERT INTO lotes (id, autor, titulo, ano, descricao, comentarios,
                                  status_colecao, leiloeiro, uf, data_pregao,
                                  preco_inicial, url, visto_em, atualizado_em)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (lote["id"], ", ".join(achados), titulo, ano, lote["descricao"],
             extrair_info_adicional(lote["descricao"], titulo), status_col,
             lote["leiloeiro"], lote["uf"], lote["data_pregao"],
             lote["preco_inicial"], lote["url"], agora, agora))
        novos_alertas += 1
        if colecao_stats is not None:
            colecao_stats[status_col] = colecao_stats.get(status_col, 0) + 1

        if not deve_alertar_colecao(status_col):
            continue

        aviso_ano = f"\nAno(s) na descrição: {anos_str}" if anos_str != "indefinido" \
            else "\n⚠️ Ano não identificado na descrição — confira manualmente"
        rotulo_col = rotulo_status_colecao(status_col, reg_col)
        enviar_telegram(
            "📚 Novo lote encontrado!\n"
            f"Autor(es): {', '.join(achados)}\n"
            f"{lote['descricao'][:300]}\n"
            f"Lance inicial: R$ {lote['preco_inicial'] or '?'}\n"
            f"Pregão: {lote['data_pregao'] or '?'} ({lote['uf']}) — "
            f"{lote['leiloeiro'] or 'leiloeiro n/d'}"
            f"{aviso_ano}\n"
            + (f"{rotulo_col}\n" if rotulo_col else "")
            + f"{lote['url']}",
            dry_run=args.dry_run)
    con.commit()
    print(f"Novos lotes com autores da lista: {novos_alertas}")
    print(f"Descartados por serem edição >= {ANO_LIMITE}: {descartados_epoca}")
    if colecao_stats:
        print("Por situação na coleção:", colecao_stats)

    print("Verificando lotes pendentes de valor final...")
    pendentes = con.execute(
        "SELECT id, url, autor FROM lotes WHERE status='em_andamento'"
    ).fetchall()
    ids_ativos = {l["id"] for l in lotes}
    for id_, url, aut in pendentes:
        if id_ in ids_ativos:
            continue
        valor, status = buscar_valor_final(sessao, url)
        time.sleep(PAUSA_ENTRE_PAGINAS)
        if status:
            con.execute(
                "UPDATE lotes SET valor_final=?, status=?, atualizado_em=? "
                "WHERE id=?", (valor, status, agora, id_))
            if status == "vendido" and valor:
                enviar_telegram(
                    f"🔨 Arrematado — {aut}\nValor final: R$ {valor}\n{url}",
                    dry_run=args.dry_run)
        else:
            con.execute(
                "UPDATE lotes SET status='encerrado_sem_dado', atualizado_em=? "
                "WHERE id=?", (agora, id_))
    con.commit()

    exportar_csv(con)
    con.close()
    print("Concluído.")


if __name__ == "__main__":
    main()
