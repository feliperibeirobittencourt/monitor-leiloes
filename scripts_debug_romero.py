import sys, re, json
sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright
from monitor_leiloes import (
    criar_contexto_navegador,
    buscar_pagina_andamento_renderizada,
    extrair_lotes,
    casar_autores,
    carregar_autores,
    parece_nao_livro,
    classificar_epoca,
    BASE,
)
from urllib.parse import quote

autores = carregar_autores("autores.txt")
nome = "Sílvio Romero"

out = []
def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    out.append(s)

with sync_playwright() as p:
    navegador = p.chromium.launch()
    contexto = criar_contexto_navegador(navegador)
    offset = 0
    pag = 1
    achou_gouvea = False
    while pag <= 15:
        url = f"{BASE}/busca_andamento.asp?op=2&b={offset}&ga=*&uf=*&pesquisa={quote(nome)}"
        html = buscar_pagina_andamento_renderizada(contexto, url)
        if not html:
            log(f"pagina {pag}: HTML vazio, parando.")
            break
        lotes = extrair_lotes(html)
        log(f"pagina {pag} (offset={offset}): {len(lotes)} lotes")
        if not lotes:
            log("  (lista vazia, parando)")
            break
        for l in lotes:
            desc_lower = l["descricao"].lower()
            if "ethnographia" in desc_lower or "gouvea" in (l.get("url","")+desc_lower).lower():
                achou_gouvea = True
                log("  >>> ACHOU candidato Gouvea/Ethnographia:", json.dumps(l, ensure_ascii=False)[:600])
        # mostra os primeiros 2 lotes de cada pagina como amostra
        for l in lotes[:2]:
            log("   amostra:", l.get("descricao","")[:100], "|", l.get("url","")[:80])
        offset += len(lotes)
        pag += 1
    contexto.close()
    navegador.close()

log("\nachou_gouvea/ethnographia nas paginas verificadas:", achou_gouvea)

with open("debug_romero_output.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out))
