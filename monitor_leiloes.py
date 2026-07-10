#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Monitor de leilões de livros — LeilõesBR (leiloesbr.com.br)
============================================================
O que faz, a cada execução:
  1. Percorre todas as páginas da categoria "Livros" (leilões em andamento).
  2. Casa título/descrição dos lotes com a lista de autores (autores.txt),
     ignorando acentos e variações comuns de grafia (Ruy/Rui, Souza/Sousa...).
  3. Lotes novos -> alerta no Telegram + gravação no banco SQLite.
  4. Revisita lotes já encerrados para capturar o valor final (arremate).
  5. Exporta tudo para planilha CSV (abre no Excel/Google Sheets).

Uso:
  pip install requests beautifulsoup4
  export TELEGRAM_BOT_TOKEN="123456:ABC..."   (crie um bot com o @BotFather)
  export TELEGRAM_CHAT_ID="123456789"          (obtenha com o @userinfobot)
  python3 monitor_leiloes.py            # execução normal
  python3 monitor_leiloes.py --dry-run  # sem enviar Telegram (teste)

Agende 1-2x por dia (cron, GitHub Actions ou PythonAnywhere).
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

import requests
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------------
# Configuração
# ----------------------------------------------------------------------------
BASE = "https://www.leiloesbr.com.br"
# Categoria "Livros" (o parâmetro tp é o nome da categoria em hexadecimal latin-1)
CAT_LIVROS_HEX = "4C6976726F73"  # "Livros"
# op=2 = leilões em andamento (padrão da busca); v=126 itens por página
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
PAUSA_ENTRE_PAGINAS = 3  # segundos — seja gentil com o servidor
MAX_PAGINAS = 60         # trava de segurança

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

# Variantes de grafia (grafia moderna -> outras grafias encontradas em edições
# antigas). A comparação já ignora acentos, maiúsculas, y/i e z/s.
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


# ----------------------------------------------------------------------------
# Normalização e casamento de nomes
# ----------------------------------------------------------------------------
def normalizar(texto: str) -> str:
    """minúsculas, sem acentos, y->i, z->s, sem pontuação, espaços únicos."""
    t = unicodedata.normalize("NFKD", texto)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower().replace("y", "i").replace("z", "s")
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def carregar_autores(caminho: str):
    """Retorna lista de (nome_original, [padroes_normalizados])."""
    autores = []
    with open(caminho, encoding="utf-8") as f:
        nomes = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    for nome in nomes:
        padroes = {normalizar(nome)}
        for var in VARIANTES.get(nome, []):
            padroes.add(normalizar(var))
        autores.append((nome, sorted(padroes)))
    return autores


def casar_autores(texto: str, autores) -> list:
    """Retorna os nomes de autores presentes no texto (busca por frase inteira)."""
    t = " " + normalizar(texto) + " "
    encontrados = []
    for nome, padroes in autores:
        if any((" " + p + " ") in t for p in padroes):
            encontrados.append(nome)
    return encontrados


# ----------------------------------------------------------------------------
# Raspagem
# ----------------------------------------------------------------------------
def buscar_pagina(sessao: requests.Session, pag: int) -> str:
    url = URL_ANDAMENTO.format(cat=CAT_LIVROS_HEX, pag=pag)
    r = sessao.get(url, headers=HEADERS, timeout=40)
    r.raise_for_status()
    return r.text


RE_PRECO = re.compile(r"R\$\s*([\d\.]+,\d{2})")
RE_DATA = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})\s*-\s*([\d:]+h?)[^A-Z]*([A-Z]{2})?")
RE_LOTE = re.compile(r"abre_catalogo\.asp\?t=\d+\|([^|]+)\|(\d+)\|(\d+)")


def extrair_lotes(html: str) -> list:
    """
    Extrai lotes da página de busca. Cada card tem um link para
    abre_catalogo.asp?t=1|<site do leiloeiro>|<id_leilao>|<id_lote>
    com a descrição completa no atributo title, e preço/data/galeria por perto.
    """
    soup = BeautifulSoup(html, "html.parser")
    lotes = {}
    for a in soup.find_all("a", href=RE_LOTE):
        m = RE_LOTE.search(a.get("href", ""))
        if not m:
            continue
        site_leiloeiro, id_leilao, id_lote = m.group(1), m.group(2), m.group(3)
        chave = f"{id_leilao}-{id_lote}"
        titulo = (a.get("title") or a.get_text(" ", strip=True) or "").strip()
        # fica com a versão mais longa da descrição encontrada para o lote
        if chave in lotes and len(titulo) <= len(lotes[chave]["descricao"]):
            continue
        # contexto: sobe até o container do card para achar preço/data/galeria
        contexto = a
        preco = data_str = uf = galeria = ""
        for _ in range(6):
            contexto = contexto.parent
            if contexto is None:
                break
            txt = contexto.get_text(" ", strip=True)
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
        lotes[chave] = {
            "id": chave,
            "descricao": titulo,
            "preco_inicial": preco,
            "data_pregao": data_str.strip(),
            "uf": uf,
            "leiloeiro": galeria,
            "url": f"{BASE}/abre_catalogo.asp?t=1|{site_leiloeiro}|{id_leilao}|{id_lote}",
        }
    return list(lotes.values())


def raspar_andamento(sessao: requests.Session) -> list:
    """Percorre todas as páginas da categoria Livros e devolve todos os lotes."""
    todos, vistos = [], set()
    for pag in range(1, MAX_PAGINAS + 1):
        try:
            html = buscar_pagina(sessao, pag)
        except requests.RequestException as e:
            print(f"[aviso] falha na página {pag}: {e}", file=sys.stderr)
            break
        lotes = extrair_lotes(html)
        novos = [l for l in lotes if l["id"] not in vistos]
        if not novos:  # fim da paginação
            break
        for l in novos:
            vistos.add(l["id"])
        todos.extend(novos)
        print(f"  página {pag}: {len(novos)} lotes")
        time.sleep(PAUSA_ENTRE_PAGINAS)
    return todos


def buscar_valor_final(sessao: requests.Session, url_lote: str):
    """
    Revisita a página do lote após o pregão para tentar capturar o valor final.
    Retorna (valor, status) ou (None, None) se não conseguir determinar.
    """
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


# ----------------------------------------------------------------------------
# Banco de dados e planilha
# ----------------------------------------------------------------------------
def abrir_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS lotes (
            id            TEXT PRIMARY KEY,
            autores       TEXT,
            descricao     TEXT,
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
    con.commit()
    return con


def exportar_csv(con):
    cur = con.execute("""SELECT autores, descricao, leiloeiro, uf, data_pregao,
                                preco_inicial, valor_final, status, url, visto_em
                         FROM lotes ORDER BY visto_em DESC""")
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Autor(es)", "Descrição", "Leiloeiro", "UF", "Data do pregão",
                    "Lance inicial (R$)", "Valor final (R$)", "Status", "Link",
                    "Detectado em"])
        w.writerows(cur.fetchall())
    print(f"Planilha atualizada: {CSV_PATH}")


# ----------------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------------
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


# ----------------------------------------------------------------------------
# Fluxo principal
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="não envia Telegram; apenas imprime")
    args = ap.parse_args()

    autores = carregar_autores(AUTORES_PATH)
    print(f"Monitorando {len(autores)} autores.")
    con = abrir_db()
    sessao = requests.Session()
    agora = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # 1) Lotes em andamento -----------------------------------------------
    print("Raspando categoria Livros (em andamento)...")
    lotes = raspar_andamento(sessao)
    print(f"Total de lotes na categoria: {len(lotes)}")

    novos_alertas = 0
    for lote in lotes:
        achados = casar_autores(lote["descricao"], autores)
        if not achados:
            continue
        ja_existe = con.execute(
            "SELECT 1 FROM lotes WHERE id=?", (lote["id"],)).fetchone()
        if ja_existe:
            con.execute("UPDATE lotes SET atualizado_em=? WHERE id=?",
                        (agora, lote["id"]))
            continue
        con.execute(
            """INSERT INTO lotes (id, autores, descricao, leiloeiro, uf,
                                  data_pregao, preco_inicial, url,
                                  visto_em, atualizado_em)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (lote["id"], ", ".join(achados), lote["descricao"],
             lote["leiloeiro"], lote["uf"], lote["data_pregao"],
             lote["preco_inicial"], lote["url"], agora, agora))
        novos_alertas += 1
        enviar_telegram(
            "📚 Novo lote encontrado!\n"
            f"Autor(es): {', '.join(achados)}\n"
            f"{lote['descricao'][:300]}\n"
            f"Lance inicial: R$ {lote['preco_inicial'] or '?'}\n"
            f"Pregão: {lote['data_pregao'] or '?'} ({lote['uf']}) — "
            f"{lote['leiloeiro'] or 'leiloeiro n/d'}\n"
            f"{lote['url']}",
            dry_run=args.dry_run)
    con.commit()
    print(f"Novos lotes com autores da lista: {novos_alertas}")

    # 2) Captura de valores finais ----------------------------------------
    print("Verificando lotes pendentes de valor final...")
    pendentes = con.execute(
        "SELECT id, url, autores FROM lotes WHERE status='em_andamento'"
    ).fetchall()
    ids_ativos = {l["id"] for l in lotes}
    for id_, url, aut in pendentes:
        if id_ in ids_ativos:
            continue  # ainda em andamento
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
            # some do catálogo sem página acessível: marca como encerrado s/ dado
            con.execute(
                "UPDATE lotes SET status='encerrado_sem_dado', atualizado_em=? "
                "WHERE id=?", (agora, id_))
    con.commit()

    # 3) Planilha -----------------------------------------------------------
    exportar_csv(con)
    con.close()
    print("Concluído.")


if __name__ == "__main__":
    main()
