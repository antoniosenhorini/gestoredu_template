"""Leitura do PDF de horario dos professores (formato "Relatorio por Professor").

Modulo sem acesso a banco de dados: recebe um arquivo PDF e devolve apenas
estruturas Python simples. O horario dos professores em si nunca e gravado
nem alterado a partir daqui - o PDF e usado somente como referencia de leitura.
"""
import re

import pdfplumber

DIAS_SEMANA_PDF = ['Segunda-feira', 'Terca-feira', 'Quarta-feira', 'Quinta-feira', 'Sexta-feira']
HEADING_AGUARDANDO_PROFESSOR = 'AGUARDANDO PROFESSOR'


def _linhas_entre(palavras, top_min, top_max):
    """Reconstroi linhas de texto (ordenadas por posicao) entre duas coordenadas verticais."""
    grupos = {}
    for palavra in palavras:
        if top_min <= palavra['top'] < top_max:
            grupos.setdefault(round(palavra['top'], 1), []).append(palavra)

    linhas = []
    for top in sorted(grupos):
        ordenadas = sorted(grupos[top], key=lambda p: p['x0'])
        linhas.append(' '.join(p['text'] for p in ordenadas))
    return linhas


def _detectar_turno(texto_primeira_pagina):
    match = re.search(r'Quadro:\s*(MANH[ÃA]|TARDE)', texto_primeira_pagina or '', re.IGNORECASE)
    if not match:
        return ''
    valor = match.group(1).upper()
    return 'manha' if valor.startswith('MANH') else 'tarde'


def _interpretar_celula(heading, dia, horario, celula_bruta):
    texto = (celula_bruta or '').strip()
    eh_aguardando = heading.strip().upper() == HEADING_AGUARDANDO_PROFESSOR

    slot = {
        'heading_pdf_texto': heading,
        'professor_pdf_texto': '' if eh_aguardando else heading,
        'dia_semana': dia,
        'horario': horario,
        'turma_pdf_texto': '',
        'disciplina_pdf_texto': '',
        'situacao_pdf': 'livre',
    }

    if texto in ('', '[X]'):
        slot['situacao_pdf'] = 'livre'
        return slot

    if texto == '[H.A.]':
        slot['situacao_pdf'] = 'ha'
        return slot

    linhas_celula = [linha.strip() for linha in texto.split('\n') if linha.strip()]
    if not linhas_celula:
        slot['situacao_pdf'] = 'livre'
        return slot

    slot['turma_pdf_texto'] = linhas_celula[0]
    slot['disciplina_pdf_texto'] = ' '.join(linhas_celula[1:])
    slot['situacao_pdf'] = 'aguardando_professor' if eh_aguardando else 'aula'
    return slot


def ler_pdf_horario_professores(arquivo):
    """Le um PDF no formato "Relatorio por Professor" e devolve os slots encontrados.

    Retorna uma tupla (turno_detectado, slots, avisos):
    - turno_detectado: 'manha', 'tarde' ou '' se nao identificado no cabecalho do PDF.
    - slots: lista de dicts, um por celula relevante da grade (ver _interpretar_celula).
    - avisos: lista de strings descrevendo trechos que o parser nao conseguiu interpretar
      com confianca (nao interrompem a leitura do restante do arquivo).
    """
    slots = []
    avisos = []
    turno_detectado = ''
    ultimo_heading = ''

    with pdfplumber.open(arquivo) as pdf:
        if pdf.pages:
            turno_detectado = _detectar_turno(pdf.pages[0].extract_text())

        for indice_pagina, pagina in enumerate(pdf.pages):
            tabelas = pagina.find_tables()
            if not tabelas:
                continue

            palavras = pagina.extract_words()
            posicao_anterior = 0

            for tabela in tabelas:
                topo_tabela = tabela.bbox[1]
                linhas_heading = _linhas_entre(palavras, posicao_anterior, topo_tabela)
                posicao_anterior = tabela.bbox[3]

                heading = linhas_heading[-1].strip() if linhas_heading else ultimo_heading
                if not heading:
                    avisos.append(f'Pagina {indice_pagina + 1}: tabela sem professor identificado, ignorada.')
                    continue
                ultimo_heading = heading

                linhas_tabela = tabela.extract()
                if not linhas_tabela or len(linhas_tabela) < 2:
                    avisos.append(f'Pagina {indice_pagina + 1}: tabela de "{heading}" vazia ou incompleta.')
                    continue

                linhas_dados = linhas_tabela[1:]
                if len(linhas_dados) != len(DIAS_SEMANA_PDF) + 1:
                    avisos.append(
                        f'Pagina {indice_pagina + 1}: tabela de "{heading}" tem '
                        f'{len(linhas_dados)} linhas de horario (esperado 6).'
                    )

                for indice_linha, linha in enumerate(linhas_dados[:6], start=1):
                    horario = str(indice_linha)
                    celulas_dias = linha[1:1 + len(DIAS_SEMANA_PDF)]
                    for indice_dia, celula in enumerate(celulas_dias):
                        dia = DIAS_SEMANA_PDF[indice_dia]
                        slots.append(_interpretar_celula(heading, dia, horario, celula))

    return turno_detectado, slots, avisos
