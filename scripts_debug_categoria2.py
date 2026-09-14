import sys
sys.path.insert(0, ".")
from monitor_leiloes import extrair_lotes, BASE, HEADERS, CAT_LIVROS_HEX
import requests

sessao = requests.Session()

def testar(nome, url):
    try:
        r = sessao.get(url, headers=HEADERS, timeout=40)
        r.raise_for_status()
    except Exception as e:
        print(f"[{nome}] ERRO: {e}")
        return None
    lotes = extrair_lotes(r.text)
    print(f"[{nome}] status={r.status_code} len_html={len(r.text)} lotes={len(lotes)}")
    for l in lotes[:3]:
        print("    ", l["descricao"][:100])
    return lotes

# 1) Baseline conhecido: categoria Livros, sem termo de busca -> deve trazer livros normalmente
testar("Livros / sem termo", f"{BASE}/busca_andamento.asp?pesquisa=&op=2&v=126&tp=|{CAT_LIVROS_HEX}|&b=0&pag=1")

# 2) Categoria Livros + termo de busca ASCII (Romero) -- pesquisa funciona junto com tp valido?
testar("Livros / Romero", f"{BASE}/busca_andamento.asp?pesquisa=Romero&op=2&v=126&tp=|{CAT_LIVROS_HEX}|&b=0&pag=1")

# 3) Sem parametro tp= nenhum (removido da URL), termo ASCII
testar("sem tp / Romero", f"{BASE}/busca_andamento.asp?pesquisa=Romero&op=2&v=126&b=0&pag=1")

# 4) Categoria "Livros Raros" (hex chutado) de novo, isolado, para comparar com o fallback de tp invalido
CAT_RAROS_HEX = "Livros Raros".encode("utf-8").hex().upper()
testar("Livros Raros (hex chutado)", f"{BASE}/busca_andamento.asp?pesquisa=&op=2&v=126&tp=|{CAT_RAROS_HEX}|&b=0&pag=1")

# 5) tp= propositalmente invalido/lixo, para ver se cai no mesmo fallback de 12 itens
testar("tp=LIXO123 (invalido de proposito)", f"{BASE}/busca_andamento.asp?pesquisa=&op=2&v=126&tp=|4C4958463132335858|&b=0&pag=1")
