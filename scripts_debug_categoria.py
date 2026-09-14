import sys
sys.path.insert(0, ".")
from monitor_leiloes import extrair_lotes, BASE, HEADERS
import requests
import re

sessao = requests.Session()

# Busca textual direta (sem filtro de categoria) pelo livro relatado pelo usuário.
for termo in ["Ethnographia", "Sylvio+Rom%E9ro", "Romero"]:
    url = f"{BASE}/busca_andamento.asp?pesquisa={termo}&op=2&v=126&tp=|TODOS|&b=0&pag=1"
    try:
        r = sessao.get(url, headers=HEADERS, timeout=40)
        r.raise_for_status()
    except Exception as e:
        print(f"termo={termo!r} ERRO: {e}")
        continue
    lotes = extrair_lotes(r.text)
    print(f"termo={termo!r} -> {len(lotes)} lotes | status={r.status_code} | len_html={len(r.text)}")
    for l in lotes[:10]:
        print("   ", l["descricao"][:120], "|", l["url"])

# Tenta sem filtro nenhum de categoria (removendo tp=)
url2 = f"{BASE}/busca_andamento.asp?pesquisa=Ethnographia&op=2&v=126&b=0&pag=1"
r2 = sessao.get(url2, headers=HEADERS, timeout=40)
r2.raise_for_status()
lotes2 = extrair_lotes(r2.text)
print(f"\nsem tp= -> {len(lotes2)} lotes")
for l in lotes2[:10]:
    print("   ", l["descricao"][:120], "|", l["url"])

with open("debug_categoria_output.txt", "w", encoding="utf-8") as f:
    f.write(f"sem_tp_total={len(lotes2)}\n")
    for l in lotes2:
        f.write(l["descricao"][:200].replace("\n", " ") + " | " + l["url"] + "\n")
    # se achou o lote, procura links de categoria (tp=) na página crua próximos ao card
    if "ethnographia" in r2.text.lower():
        idx = r2.text.lower().find("ethnographia")
        trecho = r2.text[max(0, idx-3000):idx+1000]
        f.write("\n--- TRECHO HTML PROXIMO AO LOTE ---\n")
        f.write(trecho)
        cats = re.findall(r"tp=\|([^|]+)\|", trecho)
        f.write(f"\n\n--- tp= encontrados no trecho: {cats} ---\n")
    else:
        f.write("\nEthnographia NAO encontrado na busca textual sem filtro de categoria (pode ja ter sido arrematado/removido).\n")
