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
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

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

# Filtro de época: descarta lotes cuja descrição cite claramente um ano
# igual ou posterior a este (edição/reimpressão recente). Lotes sem
# nenhum ano identificável na descrição são mantidos (para não arriscar
# perder um exemplar raro só por falta de informação no anúncio), mas
# ficam marcados na planilha para você conferir manualmente.
ANO_LIMITE = 1940

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

# Login no LeilõesBR (necessário para ver o valor final de leilões encerrados).
# Mesmo email/senha funciona em todos os leiloeiros, pois o login é
# centralizado no domínio principal leiloesbr.com.br.
LEILOESBR_EMAIL = os.environ.get("LEILOESBR_EMAIL", "")
LEILOESBR_SENHA = os.environ.get("LEILOESBR_SENHA", "")

# Link "Publicar na Web" (CSV) da sua planilha de coleção, aba TODOS.
# É um link público de só-leitura gerado pelo próprio Google Sheets —
# não expõe edição, só os dados. Se vazio, a comparação com a coleção
# é simplesmente pulada (o resto do script funciona normalmente).
COLECAO_CSV_URL = os.environ.get("COLECAO_CSV_URL", "")

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
    # Variantes de grafia pré-reforma ortográfica (comuns em catálogos de
    # leilão antigos) e um caso de pseudônimo/título nobiliárquico,
    # pesquisadas e confirmadas — mas não é uma auditoria exaustiva dos
    # 81 nomes; adicione mais aqui se notar outras.
    "Franklin Dória": ["Barão de Loreto"],
    "Coelho Neto": ["Coelho Netto"],
    "Sílvio Romero": ["Sylvio Romero"],
    "Artur Azevedo": ["Arthur Azevedo"],
    "Artur de Oliveira": ["Arthur de Oliveira"],
    "Clóvis Beviláqua": ["Clóvis Bevilacqua", "Clovis Bevilaqua"],
    "Teófilo Dias": ["Theophilo Dias"],
    "Tomás Antônio Gonzaga": ["Thomaz Antonio Gonzaga"],
    "Joaquim Manuel de Macedo": ["Joaquim Manoel de Macedo"],
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


def gerar_variantes_reversas(nome_normalizado: str) -> set:
    """
    Catálogos de livros raros costumam escrever o autor como
    'SOBRENOME, Nome' (ex.: 'ASSIS, Machado de' em vez de
    'Machado de Assis'). Gera esses padrões invertidos considerando o
    sobrenome como a última palavra (comum) ou as duas últimas
    (sobrenomes compostos, ex.: 'Porto-Alegre', 'Rio Branco').
    """
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
    """Retorna lista de (nome_original, [padroes_normalizados])."""
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
    """Retorna os nomes de autores presentes no texto (busca por frase inteira)."""
    t = " " + normalizar(texto) + " "
    encontrados = []
    for nome, padroes in autores:
        if any((" " + p + " ") in t for p in padroes):
            encontrados.append(nome)
    return encontrados


# ----------------------------------------------------------------------------
# Filtro de ano/época
# ----------------------------------------------------------------------------
RE_ANO = re.compile(r"\b(1[5-9]\d{2}|20[0-2]\d)\b")  # anos de 1500 a 2029


def extrair_anos(texto: str) -> list:
    """Todos os anos plausíveis (1500-2029) citados na descrição do lote."""
    return [int(a) for a in RE_ANO.findall(texto)]


def classificar_epoca(descricao: str):
    """
    Decide se o lote passa no filtro de época.
    Retorna (passa: bool, anos_encontrados: str).
    Regras:
      - Se o ANO MAIS RECENTE citado na descrição for >= ANO_LIMITE,
        entende-se que é uma edição/reimpressão moderna e descarta.
      - Se NENHUM ano for encontrado na descrição, também descarta —
        na prática, anúncios sem nenhuma data costumam ser edições
        recentes que o leiloeiro não se deu ao trabalho de datar, e não
        vale a pena nem registrar nem alertar sobre eles.
    """
    anos = extrair_anos(descricao)
    if not anos:
        return False, "indefinido"
    mais_recente = max(anos)
    passa = mais_recente < ANO_LIMITE
    return passa, ", ".join(str(a) for a in sorted(set(anos)))


NAO_LIVRO_TERMOS = [
    "moeda", "moedas", "reis", "selo", "selos", "filatelia",
    "filatelica", "numismatica", "medalha", "medalhas", "cedula",
    "cedulas", "flor de cunho", "bronze aluminio", "postal circulado",
    "cartao postal",
]


def parece_nao_livro(descricao: str) -> bool:
    """
    Filtro de conteúdo (feito no nosso código, não pelo site): descarta
    itens que claramente são moedas, selos, medalhas ou cédulas — não
    livros — mesmo que mencionem o nome do autor. Isso é comum: existe
    uma moeda comemorativa de 500 réis (1939) e selos postais para vários
    dos seus autores, celebrando centenários.

    Necessário porque a busca de leilões finalizados não tem mais filtro
    de categoria do site (tivemos que tirar por outro motivo — sem isso,
    a busca de "Olavo Bilac" não achava nada), então agora ela também
    traz peças de outras categorias (numismática, filatelia etc.).
    """
    t = " " + normalizar(descricao) + " "
    return any(f" {termo} " in t for termo in NAO_LIVRO_TERMOS)


CARTA_DOCUMENTO_TERMOS = [
    "carta", "cartas", "correspondencia", "documento", "documentos",
    "assinatura", "autografo", "autografos",
]


def parece_carta_documento(descricao: str) -> bool:
    """
    Identifica cartas, correspondências, documentos e autógrafos —
    diferente de moedas/selos (que são descartados sem dó), esses itens
    continuam sendo registrados e alertados normalmente, só marcados
    numa coluna própria ('Tipo') para você filtrar fácil na planilha
    depois, já que não são exatamente livros mas ainda interessam.
    """
    t = " " + normalizar(descricao) + " "
    return any(f" {termo} " in t for termo in CARTA_DOCUMENTO_TERMOS)


# ----------------------------------------------------------------------------
# Extração de detalhes bibliográficos da descrição (melhor esforço)
# ----------------------------------------------------------------------------
# IMPORTANTE: descrições de leilão não têm formato padronizado, então estas
# extrações funcionam quando a informação está escrita de forma reconhecível
# e devolvem vazio quando não conseguem identificar com segurança. A coluna
# 'descricao' sempre preserva o texto completo para conferência manual.

RE_EDICAO_NUM = re.compile(
    r"\b(\d{1,2})\s*[ªa°]?\s*\.?\s*ed(?:i[cç][ãa]o)?\b", re.IGNORECASE)
ORDINAIS_EDICAO = {"primeira": 1, "segunda": 2, "terceira": 3,
                   "quarta": 4, "quinta": 5}


def extrair_edicao(descricao: str) -> str:
    """'1ª edição', '2ªEd', '1. ed.', 'primeira edição' -> '1ª', '2ª'..."""
    m = RE_EDICAO_NUM.search(descricao)
    if m:
        return f"{m.group(1)}ª"
    baixo = normalizar(descricao)
    for palavra, n in ORDINAIS_EDICAO.items():
        if f"{palavra} edicao" in baixo:
            return f"{n}ª"
    return ""


EDITORAS_CONHECIDAS = [
    "B. L. Garnier", "H. Garnier", "Garnier", "Laemmert", "Francisco Alves",
    "Livraria do Globo", "José Olympio", "Jose Olympio", "W. M. Jackson",
    "Companhia Editora Nacional", "Imprensa Nacional", "Livraria Martins",
    "Briguiet", "Civilização Brasileira", "Norte Editora", "Teixeira",
]
RE_EDITORA_GENERICA = re.compile(
    r"(?:Editora|Livraria|Typographia|Tipografia|Typ\.|Ed\.)\s*:?\s+"
    r"([A-ZÀ-Ú][\w\.&à-úÀ-Ú' -]{2,40}?)(?=[,;.]|\s+\d|\s*$)",
    re.UNICODE)


def extrair_editora(descricao: str) -> str:
    """Editoras históricas conhecidas primeiro; senão, padrão 'Editora X'."""
    for ed in EDITORAS_CONHECIDAS:
        if ed.lower() in descricao.lower():
            return ed
    m = RE_EDITORA_GENERICA.search(descricao)
    if m:
        return m.group(1).strip(" .,;-")
    return ""


CIDADES_PUBLICACAO = [
    "Rio de Janeiro", "São Paulo", "Sao Paulo", "Porto Alegre",
    "Belo Horizonte", "Recife", "Salvador", "Curitiba", "Brasília",
    "Brasilia", "Fortaleza", "Paris", "Lisboa", "Porto", "Coimbra",
    "Milano", "Roma", "Londres", "London", "New York", "Nova York",
    "Bruxelas", "Madrid", "Buenos Aires",
]


def extrair_cidade(descricao: str) -> str:
    baixo = normalizar(descricao)
    for cid in CIDADES_PUBLICACAO:
        if normalizar(cid) in baixo:
            if cid == "Sao Paulo":
                return "São Paulo"
            if cid == "Brasilia":
                return "Brasília"
            return cid
    return ""


ASSINADO_TERMOS = ["assinado", "assinada", "autografado", "autografada",
                   "autografo", "dedicatoria", "assinatura"]


def eh_assinado(descricao: str) -> bool:
    """Menciona assinatura, autógrafo ou dedicatória do autor?"""
    baixo = normalizar(descricao)
    return any(normalizar(t) in baixo for t in ASSINADO_TERMOS)


def extrair_detalhes(descricao: str) -> dict:
    """Reúne todas as extrações bibliográficas num dicionário só."""
    edicao = extrair_edicao(descricao)
    return {
        "edicao": edicao,
        "primeira_edicao": 1 if edicao == "1ª" else 0,
        "editora": extrair_editora(descricao),
        "cidade": extrair_cidade(descricao),
        "assinado": 1 if eh_assinado(descricao) else 0,
    }


def extrair_titulo(descricao: str) -> str:
    """
    Melhor esforço para extrair o título da obra a partir da descrição do
    lote. Descrições de leilão têm formatos bem variados, então isto NÃO
    é perfeito — quando não há confiança suficiente, devolve string vazia
    e o texto completo continua preservado na coluna 'descricao'.
    Reconhece três padrões comuns observados nos anúncios do site:
      1) Título entre aspas: ...obra "O Alienista", de Machado de Assis...
      2) "SOBRENOME, Nome. - TÍTULO." (catálogos de livros raros)
      3) "Autor, Título, Ano." (formato mais simples)
    """
    txt = descricao.strip()

    m = re.search(r'["“]([^"”]{3,100})["”]', txt)
    if m:
        return m.group(1).strip()

    m = re.match(r'^[A-ZÀ-Ú][A-ZÀ-Úa-zà-ú\.,\s]{1,45}?[-–]\s*(.+?)\.', txt)
    if m:
        candidato = m.group(1).strip(" .,-–")
        if 3 <= len(candidato) <= 120:
            return candidato

    # "TÍTULO, de Fulano, ..." ou "Título por Fulano, ..." — comum em
    # descrições mais simples. Verificado antes do padrão "Autor, Título,
    # Ano" abaixo, que é ambíguo e não reconhece a palavra "de"/"por".
    m = re.match(r'^([^,]{3,60}),\s*(?:de|por)\s+.+', txt, re.IGNORECASE)
    if not m:
        m = re.match(r'^(.{3,60}?)\s+(?:de|por)\s+[A-ZÀ-Ú].+', txt)
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
    """
    Retira o título já identificado da descrição e devolve o que sobra
    (editora, estado de conservação, dimensões, ilustrador etc.) — vira a
    coluna 'comentarios', preenchida automaticamente a cada execução com
    o que o anúncio do leilão diz sobre aquele exemplar, além de autor/
    título/ano. Não é um espaço para anotações suas.
    """
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


# ----------------------------------------------------------------------------
# Comparação com a coleção pessoal (planilha "TODOS")
# ----------------------------------------------------------------------------
STOPWORDS_TITULO = {
    "de", "da", "do", "das", "dos", "e", "a", "o", "as", "os", "em", "um",
    "uma", "para", "com", "por", "no", "na", "nos", "nas", "obras",
}


RE_TITULO_ANO_FINAL = re.compile(r",?\s*\(?\b(1[5-9]\d{2}|20[0-2]\d)\)?\.?\s*$")


def limpar_titulo_colecao(titulo: str) -> str:
    """
    Muitas linhas da planilha de coleção têm o ano da edição colado no
    final do título (ex.: 'Carícia, botânica amorosa, 1895.'). Isso
    atrapalha a comparação de similaridade com o texto do leilão, que
    quase nunca tem esse mesmo ano solto do mesmo jeito. Remove esse
    sufixo quando reconhecido; se não achar nada pra remover, devolve o
    título original sem mudança.
    """
    sem_ano = RE_TITULO_ANO_FINAL.sub("", titulo.strip()).strip().rstrip(",").strip()
    return sem_ano or titulo.strip()


def _stem_titulo(palavra: str) -> str:
    """Normalização bem simples de plural em português (ex.: 'caricias'
    -> 'caricia'), só para a comparação de similaridade não falhar por
    causa de singular/plural."""
    if len(palavra) > 4 and palavra.endswith("s"):
        return palavra[:-1]
    return palavra


def similaridade_titulos(a: str, b: str) -> float:
    """
    Similaridade grosseira entre um título/descrição de leilão e uma
    entrada da sua planilha de coleção. Mede que fração das palavras
    significativas do texto MAIS CURTO aparece no outro — funciona bem
    mesmo comparando um título curto com uma descrição de leilão longa.
    Não é uma correspondência exata: obras com título muito genérico
    podem gerar falsos positivos/negativos ocasionais.
    """
    def tokens(s):
        return {
            _stem_titulo(w) for w in normalizar(s).split()
            if len(w) > 2 and w not in STOPWORDS_TITULO and not w.isdigit()
        }
    wa, wb = tokens(a), tokens(b)
    if not wa or not wb:
        return 0.0
    inter = wa & wb
    menor = min(len(wa), len(wb))
    return len(inter) / menor


def para_ano(valor) -> int | None:
    """Converte '1955', '1955.0', 1955.0 etc. em int; None se não der."""
    try:
        return int(float(valor))
    except (ValueError, TypeError):
        return None


def carregar_colecao(url: str) -> dict:
    """
    Baixa e organiza a planilha de coleção (link 'Publicar na Web' em CSV).
    Retorna {autor_normalizado: [ {titulo, tenho, ano, valor_pago,
    data_aquisicao, comentario}, ... ]}. Se a URL não estiver configurada
    ou a busca falhar, devolve {} e o resto do script segue sem essa
    camada de comparação.
    """
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
    if i_autor is None:
        # Na sua planilha real, a coluna do nome do autor não tem
        # cabeçalho (célula em branco) — mas vem sempre logo depois da
        # coluna "CADEIRA". Usa isso como âncora para achá-la mesmo sem
        # nome de coluna.
        i_cadeira = idx("cadeira")
        if i_cadeira is not None and i_cadeira + 1 < len(cabecalho):
            i_autor = i_cadeira + 1
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
            "titulo": limpar_titulo_colecao(titulo),
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
    """
    Compara um lote de leilão com a sua coleção catalogada.
    Retorna (status, registro_correspondente_ou_None).

    Status possíveis:
      'sem_colecao'            -> planilha não configurada/disponível
      'desconhecido_novo'      -> autor conhecido, mas ESTE título não
                                   consta na sua lista — alerta máximo
      'falta_edicao_conhecida' -> título já catalogado por você, mas
                                   TENHO está vazio — falta na coleção
      'ja_tenho_outra_edicao'  -> você tem, mas de um ano diferente
      'ja_tenho_mesma_edicao'  -> você já tem exatamente esta edição
    """
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
        preco = data_str = uf = galeria = imagem_url = ""
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
            if not imagem_url:
                img = contexto.find("img")
                if img:
                    # sites com lazy loading costumam usar data-src/data-original
                    # para a imagem real e deixar src com um placeholder
                    src = (img.get("data-src") or img.get("data-original")
                           or img.get("src") or "")
                    if src and "placeholder" not in src.lower():
                        if src.startswith("//"):
                            src = "https:" + src
                        elif src.startswith("/"):
                            src = BASE + src
                        imagem_url = src
            if preco and data_str and galeria and imagem_url:
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
            "id_leilao": id_leilao,
            "id_lote_leiloeiro": id_lote,
            "descricao": titulo,
            "preco_inicial": preco,
            "status_venda": status_venda,
            "data_pregao": data_str.strip(),
            "uf": uf,
            "leiloeiro": galeria,
            "imagem_url": imagem_url,
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


def buscar_pagina_renderizada(navegador, url: str, timeout_ms: int = 40000) -> str:
    """
    Abre a URL num navegador headless (Chromium via Playwright) e devolve
    o HTML já processado pelo JavaScript da página.

    O valor "Vendido por: R$ X" só aparece depois que o JavaScript da
    própria página carrega essa informação — uma busca simples (sem
    navegador) nunca vê esse valor.

    Em vez de adivinhar quanto tempo esperar (o que se mostrou pouco
    confiável — confirmado com dados reais que vieram sem nenhum valor),
    a página espera ATIVAMENTE até o texto "Vendido por" aparecer em
    algum lugar do conteúdo, ou desiste depois de um tempo limite. A
    rolagem é mantida como reforço extra, caso alguma página específica
    ainda dependa disso.
    """
    pagina = navegador.new_page()
    try:
        try:
            pagina.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        except PlaywrightTimeoutError:
            print(f"  [aviso] timeout ao carregar a página: {url}", file=sys.stderr)
            return ""

        try:
            pagina.wait_for_function(
                "document.body && document.body.innerText.includes('Vendido por')",
                timeout=15000,
            )
        except PlaywrightTimeoutError:
            # Pode ser que a página realmente não tenha nenhum item vendido
            # nela — segue em frente mesmo assim, sem travar o backfill.
            pass

        posicao = 0
        for _ in range(60):  # trava de segurança para páginas muito longas
            altura_total = pagina.evaluate("document.body.scrollHeight")
            altura_janela = pagina.evaluate("window.innerHeight")
            if posicao >= altura_total - altura_janela:
                break
            posicao += altura_janela
            pagina.evaluate(f"window.scrollTo(0, {posicao})")
            pagina.wait_for_timeout(200)
        pagina.wait_for_timeout(600)

        return pagina.content()
    except Exception as e:
        print(f"  [aviso] falha ao renderizar página: {e}", file=sys.stderr)
        return ""
    finally:
        pagina.close()


def autenticar(sessao: requests.Session) -> bool:
    """
    Tenta logar no LeilõesBR para poder ver o valor final de leilões
    encerrados (essa informação fica escondida sem login).

    AVISO IMPORTANTE: a página de login do site usa componentes dinâmicos
    e não foi possível confirmar de antemão os nomes exatos dos campos do
    formulário. Esta função tenta as combinações mais comuns; se nenhuma
    funcionar, ela apenas retorna False (sem quebrar o resto do script) e
    imprime um aviso no log para diagnóstico.
    """
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


# ----------------------------------------------------------------------------
# Banco de dados e planilha
# ----------------------------------------------------------------------------
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
            tipo_item     TEXT DEFAULT 'livro',
            edicao        TEXT,
            editora       TEXT,
            cidade        TEXT,
            assinado      INTEGER DEFAULT 0,
            primeira_edicao INTEGER DEFAULT 0,
            imagem_url    TEXT,
            id_leilao     TEXT,
            id_lote_leiloeiro TEXT,
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

    # migração: bancos de versões anteriores do script tinham colunas
    # diferentes (autores/anos_detectados em vez de autor/titulo/ano).
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
                         ("comentarios", "TEXT"), ("status_colecao", "TEXT"),
                         ("tipo_item", "TEXT"),
                         ("edicao", "TEXT"), ("editora", "TEXT"),
                         ("cidade", "TEXT"), ("assinado", "INTEGER"),
                         ("primeira_edicao", "INTEGER"),
                         ("imagem_url", "TEXT"), ("id_leilao", "TEXT"),
                         ("id_lote_leiloeiro", "TEXT")):
        if coluna not in colunas:
            con.execute(f"ALTER TABLE lotes ADD COLUMN {coluna} {tipo}")
    con.commit()

    # classifica tipo_item (livro / carta_documento) para linhas antigas
    # gravadas antes desta coluna existir
    if "tipo_item" not in colunas:
        for id_, descricao in con.execute(
                "SELECT id, descricao FROM lotes WHERE tipo_item IS NULL"):
            tipo = ("carta_documento" if parece_carta_documento(descricao or "")
                    else "livro")
            con.execute("UPDATE lotes SET tipo_item=? WHERE id=?", (tipo, id_))
        con.commit()

    # preenche os detalhes bibliográficos para linhas antigas (o que dá
    # para extrair da descrição; imagem_url não é recuperável em
    # retrospecto — só entra para lotes vistos daqui em diante)
    if "edicao" not in colunas:
        for id_, descricao in con.execute(
                "SELECT id, descricao FROM lotes WHERE edicao IS NULL").fetchall():
            d = descricao or ""
            ed = extrair_edicao(d)
            partes = id_.split("-", 1)
            con.execute(
                "UPDATE lotes SET edicao=?, editora=?, cidade=?, assinado=?, "
                "primeira_edicao=?, id_leilao=?, id_lote_leiloeiro=? WHERE id=?",
                (ed, extrair_editora(d), extrair_cidade(d),
                 1 if eh_assinado(d) else 0,
                 1 if ed == "1ª" else 0,
                 partes[0], partes[1] if len(partes) > 1 else "", id_))
        con.commit()

    # preenche título/ano para linhas gravadas antes desta atualização
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
    cur = con.execute("""SELECT autor, titulo, ano, edicao, primeira_edicao,
                                editora, cidade, assinado,
                                status_colecao, tipo_item,
                                descricao, comentarios, leiloeiro, uf, data_pregao,
                                preco_inicial, valor_final, status,
                                url, imagem_url, id_leilao, id_lote_leiloeiro,
                                visto_em
                         FROM lotes ORDER BY visto_em DESC""")
    linhas = []
    for row in cur.fetchall():
        row = list(row)
        row[4] = "sim" if row[4] else ""   # primeira_edicao
        row[7] = "sim" if row[7] else ""   # assinado
        linhas.append(row)
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Autor", "Título", "Ano", "Edição", "1ª edição?",
                    "Editora", "Cidade", "Assinado?",
                    "Situação na coleção", "Tipo",
                    "Descrição completa", "Comentários", "Leiloeiro", "UF",
                    "Data do pregão", "Lance inicial (R$)", "Valor final (R$)",
                    "Status", "Link", "Imagem", "ID leilão", "ID lote",
                    "Detectado em"])
        w.writerows(linhas)
    print(f"Planilha atualizada: {CSV_PATH}")


# ----------------------------------------------------------------------------
# Busca histórica (leilões finalizados)
# ----------------------------------------------------------------------------
def rodar_backfill(autores, sessao, con, logado, colecao, dry_run=False):
    """
    Busca automática nos LEILÕES FINALIZADOS (histórico completo do site,
    não só os que estão em andamento agora), autor por autor da sua lista.
    Aplica os mesmos filtros de autor e época, e tenta capturar o valor
    final de cada lote encontrado (precisa de login para isso).

    Diferente do escaneamento diário, o backfill NÃO manda um Telegram
    por lote — seriam dezenas/centenas de mensagens de uma vez. Em vez
    disso, grava tudo silenciosamente no banco e manda UM resumo só no
    final; os detalhes ficam na planilha para você revisar com calma.
    """
    agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    total_novos = 0
    total_pesquisados = 0
    descartados_nao_livro = 0
    stats = {}

    with sync_playwright() as p:
        navegador = p.chromium.launch()
        try:
            for nome, _ in autores:
                total_pesquisados += 1
                print(f"[{total_pesquisados}/{len(autores)}] Buscando: {nome}", flush=True)
                pag = 1
                vistos_ids = set()
                falhas_seguidas = 0
                while pag <= MAX_PAGINAS:
                    url = (f"{BASE}/busca_finalizado.asp?pesquisa={quote(nome)}"
                           f"&tp=|&op=2&v=126&pag={pag}")
                    html = buscar_pagina_renderizada(navegador, url)
                    time.sleep(PAUSA_ENTRE_PAGINAS)
                    if not html:
                        falhas_seguidas += 1
                        if falhas_seguidas >= 3:
                            print(f"  [aviso] 3 falhas seguidas — desistindo "
                                  f"deste autor por ora.", flush=True)
                            break
                        print(f"  [aviso] falha na página {pag} — pulando "
                              f"e tentando a próxima.", flush=True)
                        pag += 1
                        continue
                    falhas_seguidas = 0
                    lotes = extrair_lotes(html)
                    lotes_novos_pagina = [l for l in lotes if l["id"] not in vistos_ids]
                    if not lotes_novos_pagina:
                        break  # página vazia OU repetindo resultados já vistos — encerra
                    for l in lotes_novos_pagina:
                        vistos_ids.add(l["id"])
                    print(f"  página {pag}: {len(lotes_novos_pagina)} lotes", flush=True)

                    # Otimização para reexecuções: descobre de uma vez quais
                    # ids desta página já estão no banco (de uma busca
                    # anterior) e se já têm valor final preenchido.
                    ids_pagina = [l["id"] for l in lotes_novos_pagina]
                    marcadores = ",".join("?" * len(ids_pagina))
                    ja_no_banco = {
                        row[0]: row[1] for row in con.execute(
                            f"SELECT id, valor_final FROM lotes WHERE id IN ({marcadores})",
                            ids_pagina)
                    }
                    # só para de virar página se a página inteira já era
                    # conhecida E todos já tinham valor final — assim, um
                    # lote que passou pelo escaneamento diário (sem preço
                    # ainda) não bloqueia a atualização por engano.
                    pagina_inteira_conhecida = ids_pagina and all(
                        ja_no_banco.get(i) for i in ids_pagina)

                    for lote in lotes_novos_pagina:
                        if lote["id"] in ja_no_banco:
                            # já existia (provavelmente inserido pelo
                            # escaneamento diário enquanto ainda estava em
                            # andamento) — se não tinha valor final e agora
                            # temos, só atualiza; não insere de novo.
                            if not ja_no_banco[lote["id"]] and lote["preco_inicial"]:
                                con.execute(
                                    "UPDATE lotes SET valor_final=?, status=?, "
                                    "atualizado_em=? WHERE id=?",
                                    (lote["preco_inicial"],
                                     lote["status_venda"] or "vendido",
                                     agora, lote["id"]))
                                print(f"    valor atualizado para lote já "
                                      f"conhecido: {lote['id']}", flush=True)
                            continue
                        achados = casar_autores(lote["descricao"], autores)
                        if not achados:
                            continue
                        if parece_nao_livro(lote["descricao"]):
                            descartados_nao_livro += 1
                            continue
                        passa_epoca, anos_str = classificar_epoca(lote["descricao"])
                        if not passa_epoca:
                            continue

                        valor_final = lote["preco_inicial"] or None
                        status = lote["status_venda"] or "encerrado_historico"
                        ano = (max(int(a) for a in anos_str.split(", "))
                               if anos_str != "indefinido" else None)
                        titulo = extrair_titulo(lote["descricao"])
                        texto_comparar = titulo or lote["descricao"]
                        status_col, reg_col = avaliar_contra_colecao(achados[0], texto_comparar, ano, colecao)
                        tipo_item = ("carta_documento"
                                     if parece_carta_documento(lote["descricao"])
                                     else "livro")
                        det = extrair_detalhes(lote["descricao"])

                        con.execute(
                            """INSERT INTO lotes (id, autor, titulo, ano, descricao,
                                                  comentarios, status_colecao, tipo_item,
                                                  edicao, primeira_edicao, editora,
                                                  cidade, assinado, imagem_url,
                                                  id_leilao, id_lote_leiloeiro,
                                                  leiloeiro, uf, data_pregao,
                                                  preco_inicial, valor_final, status, url,
                                                  visto_em, atualizado_em)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (lote["id"], ", ".join(achados), titulo,
                             ano, lote["descricao"],
                             extrair_info_adicional(lote["descricao"], titulo), status_col,
                             tipo_item,
                             det["edicao"], det["primeira_edicao"], det["editora"],
                             det["cidade"], det["assinado"], lote.get("imagem_url", ""),
                             lote.get("id_leilao", ""), lote.get("id_lote_leiloeiro", ""),
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
                    if pagina_inteira_conhecida:
                        print(f"  página {pag} inteira já estava no banco — "
                              f"encerrando busca deste autor.", flush=True)
                        break
                    pag += 1
        finally:
            navegador.close()

    print(f"\nBackfill concluído. Autores pesquisados: {total_pesquisados}. "
          f"Novos lotes adicionados ao histórico: {total_novos}.")
    print(f"Descartados por não parecerem livros (moeda/selo/medalha): "
          f"{descartados_nao_livro}")
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


def rotulo_status_colecao(status: str, registro: dict | None) -> str:
    """Texto amigável para colocar na mensagem de alerta."""
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
    """Não vale a pena incomodar com Telegram quando já é exatamente
    a edição que você tem — mas o lote continua sendo gravado no banco."""
    return status != "ja_tenho_mesma_edicao"


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
    ap.add_argument("--backfill", action="store_true",
                    help="busca no histórico de leilões finalizados, "
                         "autor por autor, em vez do escaneamento diário")
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

    # 1) Lotes em andamento -----------------------------------------------
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
            continue  # edição/reimpressão moderna (ano >= ANO_LIMITE) — ignora
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
        tipo_item = ("carta_documento" if parece_carta_documento(lote["descricao"])
                     else "livro")
        det = extrair_detalhes(lote["descricao"])

        con.execute(
            """INSERT INTO lotes (id, autor, titulo, ano, descricao, comentarios,
                                  status_colecao, tipo_item,
                                  edicao, primeira_edicao, editora, cidade,
                                  assinado, imagem_url, id_leilao, id_lote_leiloeiro,
                                  leiloeiro, uf, data_pregao,
                                  preco_inicial, url, visto_em, atualizado_em)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (lote["id"], ", ".join(achados), titulo, ano, lote["descricao"],
             extrair_info_adicional(lote["descricao"], titulo), status_col, tipo_item,
             det["edicao"], det["primeira_edicao"], det["editora"], det["cidade"],
             det["assinado"], lote.get("imagem_url", ""),
             lote.get("id_leilao", ""), lote.get("id_lote_leiloeiro", ""),
             lote["leiloeiro"], lote["uf"], lote["data_pregao"],
             lote["preco_inicial"], lote["url"], agora, agora))
        novos_alertas += 1
        if colecao_stats is not None:
            colecao_stats[status_col] = colecao_stats.get(status_col, 0) + 1

        if not deve_alertar_colecao(status_col):
            continue  # já tem exatamente esta edição — não incomoda no Telegram

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

    # 2) Captura de valores finais ----------------------------------------
    print("Verificando lotes pendentes de valor final...")
    pendentes = con.execute(
        "SELECT id, url, autor FROM lotes WHERE status='em_andamento'"
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
