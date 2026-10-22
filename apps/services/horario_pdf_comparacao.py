"""Compara os slots lidos de um PDF de horario com o cronograma ativo dos laboratorios.

Nao altera nenhum AgendamentoLab: o resultado inteiro vira linhas de
DivergenciaCronogramaPdf, que e o que a tela de relatorio lista e filtra.
"""
from collections import defaultdict

from ..models import AgendamentoLab, DivergenciaCronogramaPdf, ProfessorPdfAlias
from .horario_pdf_matching import resolver_professor_pdf, resolver_turma_pdf

DIAS_SEMANA_ORDEM = ['Segunda-feira', 'Terca-feira', 'Quarta-feira', 'Quinta-feira', 'Sexta-feira']


def _dia_da_semana(data):
    indice = data.weekday()
    if indice >= len(DIAS_SEMANA_ORDEM):
        return None
    return DIAS_SEMANA_ORDEM[indice]


def _procurar_sugestao(slots_do_professor, turma_alvo_id, dia_atual, horario_atual):
    """Procura, nos slots do mesmo professor, um unico outro dia/horario onde a
    turma do laboratorio aparece dando aula no PDF - vira sugestao de troca."""
    if turma_alvo_id is None:
        return ('', '')

    achados = []
    for slot in slots_do_professor:
        if slot['situacao_pdf'] != 'aula':
            continue
        if slot['dia_semana'] == dia_atual and slot['horario'] == horario_atual:
            continue
        turma_slot, _alias = resolver_turma_pdf(slot['turma_pdf_texto'])
        if turma_slot and turma_slot.id == turma_alvo_id:
            achados.append((slot['dia_semana'], slot['horario']))

    if len(achados) == 1:
        return achados[0]
    return ('', '')


def _criar_divergencia(upload, turno, dia_semana, horario, agendamento, status, detalhe,
                        professor_pdf_texto='', turma_pdf_texto='', disciplina_pdf_texto='',
                        turma_obj=None, sugestao=('', '')):
    return DivergenciaCronogramaPdf(
        upload=upload,
        agendamento_lab=agendamento,
        turno=turno,
        dia_semana=dia_semana,
        horario=horario,
        professor_pdf_texto=professor_pdf_texto or (
            agendamento.professor.nome_completo if agendamento and agendamento.professor_id else ''
        ),
        professor=agendamento.professor if agendamento and agendamento.professor_id else None,
        turma_pdf_texto=turma_pdf_texto,
        turma=turma_obj,
        disciplina_pdf_texto=disciplina_pdf_texto,
        laboratorio=agendamento.laboratorio if agendamento else None,
        status=status,
        detalhe=detalhe,
        sugestao_dia=sugestao[0],
        sugestao_horario=sugestao[1],
    )


def gerar_divergencias(upload, slots, semana_inicio, semana_fim, usuario=None):
    """Gera e persiste as DivergenciaCronogramaPdf para um upload ja lido.

    `semana_inicio`/`semana_fim` sao a semana do cronograma ATIVO do turno do
    upload (calculadas pelo chamador via `obter_cronograma_ativo`, para nao
    duplicar essa regra aqui e para nunca cruzar turnos).
    """
    turno = upload.turno_detectado

    agendamentos = list(
        AgendamentoLab.objects.filter(
            turno=turno,
            data__range=(semana_inicio, semana_fim),
        ).select_related('professor', 'turma', 'laboratorio')
    )

    agendamentos_por_chave = defaultdict(list)
    for agendamento in agendamentos:
        dia = _dia_da_semana(agendamento.data)
        if dia is None:
            continue
        agendamentos_por_chave[(agendamento.professor_id, dia, agendamento.horario)].append(agendamento)

    slots_por_professor_texto = defaultdict(list)
    headings_presentes = set()
    for slot in slots:
        if slot.get('heading_pdf_texto'):
            headings_presentes.add(slot['heading_pdf_texto'])
        if slot['professor_pdf_texto']:
            slots_por_professor_texto[slot['professor_pdf_texto']].append(slot)

    professores_resolvidos = {}

    def resolver_prof_cache(texto):
        if texto not in professores_resolvidos:
            professor, ignorar, _alias = resolver_professor_pdf(texto, usuario=usuario)
            professores_resolvidos[texto] = (professor, ignorar)
        return professores_resolvidos[texto]

    divergencias = []
    agendamentos_processados = set()

    for slot in slots:
        situacao = slot['situacao_pdf']

        if situacao == 'aguardando_professor':
            turma_obj, _alias = resolver_turma_pdf(slot['turma_pdf_texto'], usuario=usuario)
            candidatos = [
                agendamento for agendamento in agendamentos
                if _dia_da_semana(agendamento.data) == slot['dia_semana']
                and agendamento.horario == slot['horario']
                and (turma_obj is None or agendamento.turma_id == turma_obj.id)
            ]
            for agendamento in candidatos:
                agendamentos_processados.add(agendamento.id)
                divergencias.append(_criar_divergencia(
                    upload, turno, slot['dia_semana'], slot['horario'], agendamento,
                    status='verificar',
                    detalhe='O PDF ainda nao tem professor definido para esta turma/horario.',
                    turma_pdf_texto=slot['turma_pdf_texto'],
                    disciplina_pdf_texto=slot['disciplina_pdf_texto'],
                    turma_obj=turma_obj,
                ))
            continue

        if situacao != 'aula':
            continue

        texto_professor = slot['professor_pdf_texto']
        professor_obj, ignorar = resolver_prof_cache(texto_professor)

        # Resolve (e registra o alias pendente de) turma sempre, independente do
        # professor ja estar vinculado - assim a tela de vinculos pendentes mostra
        # tudo de uma vez, sem exigir um segundo upload so para revelar turmas novas.
        turma_obj, _alias = (
            resolver_turma_pdf(slot['turma_pdf_texto'], usuario=usuario)
            if slot['turma_pdf_texto'] else (None, None)
        )

        if ignorar or professor_obj is None:
            continue

        candidatos = agendamentos_por_chave.get((professor_obj.id, slot['dia_semana'], slot['horario']), [])
        if not candidatos:
            continue

        for agendamento in candidatos:
            agendamentos_processados.add(agendamento.id)

            if turma_obj is None:
                status = 'verificar'
                detalhe = f'Turma do PDF ("{slot["turma_pdf_texto"]}") ainda nao esta vinculada a um cadastro.'
                sugestao = ('', '')
            elif turma_obj.id == agendamento.turma_id:
                status = 'confere'
                detalhe = ''
                sugestao = ('', '')
            else:
                status = 'conflito'
                detalhe = 'O PDF mostra uma turma diferente da reserva atual do laboratorio.'
                sugestao = _procurar_sugestao(
                    slots_por_professor_texto.get(texto_professor, []),
                    agendamento.turma_id,
                    slot['dia_semana'],
                    slot['horario'],
                )

            divergencias.append(_criar_divergencia(
                upload, turno, slot['dia_semana'], slot['horario'], agendamento,
                status=status, detalhe=detalhe,
                professor_pdf_texto=texto_professor,
                turma_pdf_texto=slot['turma_pdf_texto'],
                disciplina_pdf_texto=slot['disciplina_pdf_texto'],
                turma_obj=turma_obj,
                sugestao=sugestao,
            ))

    # Sentido inverso: reserva do lab cujo professor tem alias resolvido e cujo
    # heading aparece neste upload, mas o PDF nao confirma aula nesse dia/horario.
    for agendamento in agendamentos:
        if agendamento.id in agendamentos_processados:
            continue
        if not agendamento.professor_id:
            continue

        alias = ProfessorPdfAlias.objects.filter(
            professor_id=agendamento.professor_id,
            texto_pdf__in=headings_presentes,
        ).first()
        if alias is None:
            continue

        dia = _dia_da_semana(agendamento.data)
        if dia is None:
            continue

        sugestao = _procurar_sugestao(
            slots_por_professor_texto.get(alias.texto_pdf, []),
            agendamento.turma_id,
            dia,
            agendamento.horario,
        )
        divergencias.append(_criar_divergencia(
            upload, turno, dia, agendamento.horario, agendamento,
            status='verificar',
            detalhe='O PDF nao confirma esta aula neste dia/horario para o professor.',
            professor_pdf_texto=alias.texto_pdf,
            sugestao=sugestao,
        ))

    DivergenciaCronogramaPdf.objects.bulk_create(divergencias)
    return divergencias
