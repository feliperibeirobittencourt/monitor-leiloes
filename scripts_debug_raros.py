import sys
sys.path.insert(0, ".")
from monitor_leiloes import buscar_pagina, extrair_lotes, BASE, HEADERS
import requests

CAT_LIVROS_RAROS_HEX = "Livros Raros".encode("utf-8").hex().upper()
print("hex categoria Livros Raros:", CAT_LIVROS_RAROS_HEX)

url = f"{BASE}/busca_andamento.asp?pesquisa=&op=2&v=126&tp=|{CAT_LIVROS_RAROS_HEX}|&b=0&pag=1"
print("URL:", url)

sessao = requests.Session()
r = sessao.get(url, headers=HEADERS, timeout=40)
r.raise_for_status()
lotes = extrair_lotes(r.text)
print(f"pagina 1: {len(lotes)} lotes")

achou = False
for l in lotes:
    if "ethnographia" in l["descricao"].lower() or "gouvea" in (l.get("url","")+l["descricao"]).lower():
        achou = True
        print(">>> ACHOU:", l["descricao"][:200], "|", l["url"])

for l in lotes[:5]:
    print("amostra:", l["descricao"][:100])

print("\nachou Ethnographia/Gouvea na pagina 1:", achou)

with open("debug_raros_output.txt", "w", encoding="utf-8") as f:
    f.write(f"hex: {CAT_LIVROS_RAROS_HEX}\nurl: {url}\ntotal_lotes_pag1: {len(lotes)}\nachou: {achou}\n")
    for l in lotes:
        f.write(l["descricao"][:150].replace("\n"," ") + " | " + l["url"] + "\n")
