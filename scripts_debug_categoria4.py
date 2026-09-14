import sys
sys.path.insert(0, ".")
from monitor_leiloes import BASE, HEADERS, CAT_LIVROS_HEX
import requests
import re

sessao = requests.Session()

url = f"{BASE}/busca_andamento.asp?pesquisa=&op=2&v=126&tp=|{CAT_LIVROS_HEX}|&b=0&pag=1"
r = sessao.get(url, headers=HEADERS, timeout=40)
r.raise_for_status()
html = r.text

# procura por qualquer outro "tp=|HEX|" na pagina (widget de categorias, links irmãos)
ocorrencias = re.findall(r"tp=\|([0-9A-Fa-f]+)\|", html)
vistos = {}
for hexcode in ocorrencias:
    vistos[hexcode] = vistos.get(hexcode, 0) + 1

with open("debug_categoria4_output.txt", "w", encoding="utf-8") as f:
    f.write(f"len_html={len(html)}\n")
    f.write(f"total ocorrencias tp=: {len(ocorrencias)}\n")
    f.write("codigos distintos encontrados (hex -> tentativa de decode utf-8 -> contagem):\n")
    for hexcode, count in sorted(vistos.items(), key=lambda x: -x[1]):
        try:
            nome = bytes.fromhex(hexcode).decode("utf-8", errors="replace")
        except Exception:
            nome = "<erro decode>"
        f.write(f"  {hexcode} -> {nome!r}  (x{count})\n")

    # tambem procura por qualquer texto "Livros Raros" cru na pagina, e o contexto ao redor
    idx = html.lower().find("raros")
    if idx != -1:
        f.write("\n--- trecho contendo 'raros' na pagina ---\n")
        f.write(html[max(0, idx-500):idx+500])
    else:
        f.write("\nPalavra 'raros' NAO encontrada na pagina da categoria Livros.\n")

print(open("debug_categoria4_output.txt", encoding="utf-8").read())
