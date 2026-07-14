#!/usr/bin/env python3
"""
Faxina única no banco de dados existente: corta descrições e comentários
gigantes que já foram gravados antes da correção (alguns leiloeiros colam
biografias inteiras no anúncio — uma chegou a ter 74 mil caracteres).

Isso não muda nenhuma lógica de busca ou comparação com a coleção — só
enxuga o texto guardado, exatamente como o script principal já faz para
capturas novas a partir de agora.

Como rodar (na pasta do repositório, junto com monitor_leiloes.py):

    python3 faxina_descricoes.py            # aplica de verdade
    python3 faxina_descricoes.py --dry-run  # só mostra o que mudaria

O script faz uma cópia de segurança do banco (leiloes.db.bak) antes de
alterar qualquer coisa.
"""
import argparse
import shutil
import sys

import monitor_leiloes as m

LIMITE_DESCRICAO = 1500


def truncar(texto: str) -> str:
    if texto and len(texto) > LIMITE_DESCRICAO:
        return texto[:LIMITE_DESCRICAO].rstrip() + "…"
    return texto


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                         help="Mostra o que seria alterado, sem gravar nada.")
    args = parser.parse_args()

    print(f"Banco: {m.DB_PATH}")
    con = m.abrir_db()

    linhas = con.execute(
        "SELECT id, descricao, comentarios FROM lotes"
    ).fetchall()
    print(f"Total de lotes no banco: {len(linhas)}")

    candidatos = []
    maior_antes = 0
    for id_, descricao, comentarios in linhas:
        nova_descricao = truncar(descricao or "")
        novo_comentario = truncar(comentarios or "")
        tamanho_original = max(len(descricao or ""), len(comentarios or ""))
        maior_antes = max(maior_antes, tamanho_original)
        if nova_descricao != (descricao or "") or novo_comentario != (comentarios or ""):
            candidatos.append((id_, nova_descricao, novo_comentario, tamanho_original))

    print(f"Maior descrição/comentário encontrado: {maior_antes:,} caracteres")
    print(f"Lotes que serão enxugados: {len(candidatos)}")

    if not candidatos:
        print("Nada para fazer — nenhum texto passa do limite de "
              f"{LIMITE_DESCRICAO} caracteres.")
        con.close()
        return

    # Mostra os 5 piores casos como amostra, para conferência.
    piores = sorted(candidatos, key=lambda x: x[3], reverse=True)[:5]
    print("\nMaiores casos encontrados (antes de cortar):")
    for id_, _, _, tamanho in piores:
        print(f"  - lote {id_}: {tamanho:,} caracteres")

    if args.dry_run:
        print("\n[--dry-run] Nada foi alterado. Rode sem essa opção para "
              "aplicar de verdade.")
        con.close()
        return

    print(f"\nFazendo backup em {m.DB_PATH}.bak ...")
    shutil.copyfile(m.DB_PATH, f"{m.DB_PATH}.bak")

    for id_, nova_descricao, novo_comentario, _ in candidatos:
        con.execute(
            "UPDATE lotes SET descricao = ?, comentarios = ? WHERE id = ?",
            (nova_descricao, novo_comentario, id_),
        )
    con.commit()
    print(f"{len(candidatos)} lotes atualizados no banco.")

    print("Gerando leiloes.csv atualizado ...")
    m.exportar_csv(con)
    con.close()
    print("\nConcluído. Se algo parecer errado, o backup está em "
          f"{m.DB_PATH}.bak — é só substituir o leiloes.db por ele de volta.")


if __name__ == "__main__":
    sys.exit(main())
