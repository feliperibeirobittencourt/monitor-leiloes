import sys
sys.path.insert(0, ".")
from monitor_leiloes import extrair_lotes, BASE, HEADERS, CAT_LIVROS_HEX
from urllib.parse import quote
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
    for l in lotes[:5]:
        print("    ", l["descricao"][:110])
    return lotes

# nome exato do autor como aparece em autores.txt, url-encoded corretamente
nome_autor = "Sílvio Romero"
testar(
    "Livros + nome completo do autor (com tp=)",
    f"{BASE}/busca_andamento.asp?pesquisa={quote(nome_autor)}&op=2&v=126&tp=|{CAT_LIVROS_HEX}|&b=0&pag=1",
)

# a mesma busca por autor, mas do jeito que o codigo de producao monta (sem tp=)
testar(
    "sem tp= + nome completo do autor (formato usado em producao)",
    f"{BASE}/busca_andamento.asp?op=2&b=0&ga=*&uf=*&pesquisa={quote(nome_autor)}",
)

# Ethnographia dentro da categoria Livros (conhecida boa) - existe em qualquer pagina?
for pag in range(1, 4):
    testar(
        f"Livros + Ethnographia pag={pag}",
        f"{BASE}/busca_andamento.asp?pesquisa=Ethnographia&op=2&v=126&tp=|{CAT_LIVROS_HEX}|&b=0&pag={pag}",
    )

# Sylvio (grafia arcaica, ASCII) dentro de Livros
testar(
    "Livros + Sylvio",
    f"{BASE}/busca_andamento.asp?pesquisa=Sylvio&op=2&v=126&tp=|{CAT_LIVROS_HEX}|&b=0&pag=1",
)
