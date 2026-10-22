"""Verificacao do cronograma dos laboratorios pelo PDF de horario dos professores.

Fluxo: upload do PDF -> leitura -> comparacao com o cronograma ATIVO do turno ->
relatorio de divergencias -> analise individual -> confirmacao explicita antes
de qualquer alteracao no AgendamentoLab. Nada e alterado automaticamente.
"""
from datetime import timedelta

from django.db import transaction

from .utilitarios import *
from .laboratorios import obter_cronograma_ativo, DIAS_SEMANA_LAB, HORARIOS_LAB

from ..models import (
    AgendamentoLab, DivergenciaCronogramaPdf, HistoricoAlteracaoCronogramaLab,
    HorarioProfessorPDF, Laboratorio, Professor, ProfessorPdfAlias, Turma, TurmaPdfAlias,
)
from ..services.horario_pdf_parser import ler_pdf_horario_professores
from ..services.horario_pdf_matching import sugerir_professor, sugerir_turma
from ..services.horario_pdf_comparacao import gerar_divergencias


STATUS_ORDEM = {'conflito': 0, 'verificar': 1, 'confere': 2}


@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def verificar_horario_pdf_upload(request):
    """Envio de um novo PDF de horario de professores para conferencia (modo diagnostico)."""
    if request.method != 'POST':
        context = {
            'uploads_recentes': HorarioProfessorPDF.objects.all()[:15],
            'pendentes_professor': ProfessorPdfAlias.objects.filter(professor__isnull=True, ignorar=False).count(),
            'pendentes_turma': TurmaPdfAlias.objects.filter(turma__isnull=True).count(),
        }
        return render(request, 'laboratorios/verificar_horario_pdf_upload.html', context)

    arquivo = request.FILES.get('arquivo')
    turno_manual = request.POST.get('turno', '').strip()

    if not arquivo or not arquivo.name.lower().endswith('.pdf'):
        messages.error(request, 'Envie um arquivo PDF valido.')
        return redirect('verificar_horario_pdf_upload')

    try:
        turno_detectado, slots, avisos = ler_pdf_horario_professores(arquivo)
    except Exception:
        messages.error(request, 'Nao foi possivel ler este PDF. Verifique se o arquivo nao esta corrompido.')
        return redirect('verificar_horario_pdf_upload')

    turno = turno_detectado or (turno_manual if turno_manual in ['manha', 'tarde'] else '')
    if not turno:
        messages.error(
            request,
            'Nao foi possivel identificar automaticamente o turno (MANHA/TARDE) deste PDF. '
            'Selecione o turno manualmente e envie novamente.'
        )
        return redirect('verificar_horario_pdf_upload')

    semana_inicio, semana_fim = obter_cronograma_ativo(turno)
    if not semana_inicio:
        messages.error(
            request,
            f'Nao ha cronograma ativo configurado para o turno da {turno}. '
            'Ative um cronograma dos laboratorios para este turno antes de conferir o PDF.'
        )
        return redirect('verificar_horario_pdf_upload')

    if not slots:
        messages.warning(request, 'O PDF foi lido, mas nenhuma informacao de horario foi encontrada nele.')

    arquivo.seek(0)
    upload = HorarioProfessorPDF.objects.create(
        arquivo=arquivo,
        nome_original=arquivo.name,
        turno_detectado=turno,
        enviado_por=request.user if request.user.is_authenticated else None,
        total_slots_lidos=len(slots),
        avisos_leitura='\n'.join(avisos),
    )

    gerar_divergencias(upload, slots, semana_inicio, semana_fim, usuario=request.user)

    messages.success(
        request,
        f'PDF "{arquivo.name}" lido com sucesso ({len(slots)} horarios encontrados). '
        'Nenhuma reserva de laboratorio foi alterada - confira as divergencias abaixo.'
    )
    return redirect('verificar_horario_pdf_relatorio', upload_id=upload.id)


@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def verificar_horario_pdf_relatorio(request, upload_id):
    """Tela de diagnostico: divergencias encontradas entre o PDF e o cronograma ativo."""
    upload = get_object_or_404(HorarioProfessorPDF, pk=upload_id)
    divergencias_qs = upload.divergencias.select_related(
        'professor', 'turma', 'laboratorio', 'agendamento_lab', 'agendamento_lab__turma',
    )

    contagem_total = divergencias_qs.count()
    contagem_confere = divergencias_qs.filter(status='confere').count()
    contagem_verificar = divergencias_qs.filter(status='verificar').count()
    contagem_conflito = divergencias_qs.filter(status='conflito').count()

    status_filtro = request.GET.get('status', '')
    professor_filtro = request.GET.get('professor', '')
    laboratorio_filtro = request.GET.get('laboratorio', '')
    dia_filtro = request.GET.get('dia', '')

    divergencias = divergencias_qs
    if status_filtro in ['confere', 'verificar', 'conflito']:
        divergencias = divergencias.filter(status=status_filtro)
    if professor_filtro:
        divergencias = divergencias.filter(professor_id=professor_filtro)
    if laboratorio_filtro:
        divergencias = divergencias.filter(laboratorio_id=laboratorio_filtro)
    if dia_filtro:
        divergencias = divergencias.filter(dia_semana=dia_filtro)

    divergencias = sorted(
        divergencias,
        key=lambda d: (STATUS_ORDEM.get(d.status, 9), DIAS_SEMANA_LAB.index(d.dia_semana) if d.dia_semana in DIAS_SEMANA_LAB else 9, d.horario),
    )

    professores_disponiveis = Professor.objects.filter(
        id__in=divergencias_qs.exclude(professor__isnull=True).values_list('professor_id', flat=True)
    ).order_by('nome_completo')
    laboratorios_disponiveis = Laboratorio.objects.filter(
        id__in=divergencias_qs.exclude(laboratorio__isnull=True).values_list('laboratorio_id', flat=True)
    ).order_by('nome')

    context = {
        'upload': upload,
        'divergencias': divergencias,
        'contagem_total': contagem_total,
        'contagem_confere': contagem_confere,
        'contagem_verificar': contagem_verificar,
        'contagem_conflito': contagem_conflito,
        'status_filtro': status_filtro,
        'professor_filtro': professor_filtro,
        'laboratorio_filtro': laboratorio_filtro,
        'dia_filtro': dia_filtro,
        'professores_disponiveis': professores_disponiveis,
        'laboratorios_disponiveis': laboratorios_disponiveis,
        'dias_semana': DIAS_SEMANA_LAB,
        'pendentes_professor': ProfessorPdfAlias.objects.filter(professor__isnull=True, ignorar=False).count(),
        'pendentes_turma': TurmaPdfAlias.objects.filter(turma__isnull=True).count(),
    }
    return render(request, 'laboratorios/verificar_horario_pdf_relatorio.html', context)


@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def verificar_horario_pdf_analisar(request, divergencia_id):
    """Mostra o cronograma atual do laboratorio vs. o horario encontrado no PDF."""
    divergencia = get_object_or_404(
        DivergenciaCronogramaPdf.objects.select_related(
            'professor', 'turma', 'laboratorio', 'upload',
            'agendamento_lab', 'agendamento_lab__turma', 'agendamento_lab__professor',
        ),
        pk=divergencia_id,
    )

    if request.method == 'POST':
        acao = request.POST.get('acao')
        if acao in ['manter', 'ignorar']:
            divergencia.decisao = 'mantido' if acao == 'manter' else 'ignorado'
            divergencia.decidido_por = request.user
            divergencia.decidido_em = timezone.now()
            divergencia.save()
            messages.success(request, 'Decisao registrada. Nenhuma reserva de laboratorio foi alterada.')
            return redirect('verificar_horario_pdf_relatorio', upload_id=divergencia.upload_id)
        messages.error(request, 'Acao invalida.')
        return redirect('verificar_horario_pdf_analisar', divergencia_id=divergencia.id)

    context = {
        'divergencia': divergencia,
        'agendamento': divergencia.agendamento_lab,
    }
    return render(request, 'laboratorios/verificar_horario_pdf_analisar.html', context)


@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def verificar_horario_pdf_confirmar(request, divergencia_id):
    """Unico ponto do sistema que efetivamente altera um AgendamentoLab a partir da conferencia por PDF.

    GET mostra o antes/depois para revisao. So o POST com o campo `confirmar=1`
    (botao "Confirmar alteracao") aplica a mudanca; qualquer outro POST cancela
    sem tocar em nada.
    """
    divergencia = get_object_or_404(
        DivergenciaCronogramaPdf.objects.select_related(
            'upload', 'turma',
            'agendamento_lab', 'agendamento_lab__turma',
            'agendamento_lab__professor', 'agendamento_lab__laboratorio',
        ),
        pk=divergencia_id,
    )
    agendamento = divergencia.agendamento_lab
    if agendamento is None:
        messages.error(request, 'Esta divergencia nao esta associada a uma reserva de laboratorio.')
        return redirect('verificar_horario_pdf_relatorio', upload_id=divergencia.upload_id)

    turma_sugerida_id = divergencia.turma_id or agendamento.turma_id
    dia_sugerido = divergencia.sugestao_dia or divergencia.dia_semana
    horario_sugerido = divergencia.sugestao_horario or divergencia.horario

    if request.method == 'POST':
        if request.POST.get('confirmar') != '1':
            messages.info(request, 'Alteracao cancelada. Nenhuma reserva foi modificada.')
            return redirect('verificar_horario_pdf_relatorio', upload_id=divergencia.upload_id)

        novo_turma_id = request.POST.get('turma_id')
        novo_dia = request.POST.get('dia_semana')
        novo_horario = request.POST.get('horario')

        if novo_dia not in DIAS_SEMANA_LAB or novo_horario not in HORARIOS_LAB or not novo_turma_id:
            messages.error(request, 'Dados invalidos para confirmar a alteracao.')
            return redirect('verificar_horario_pdf_confirmar', divergencia_id=divergencia.id)

        turma_nova = get_object_or_404(Turma, pk=novo_turma_id)
        semana_inicio, _semana_fim = obter_cronograma_ativo(agendamento.turno)
        if not semana_inicio:
            messages.error(request, 'Nao ha cronograma ativo para este turno no momento.')
            return redirect('verificar_horario_pdf_relatorio', upload_id=divergencia.upload_id)

        nova_data = semana_inicio + timedelta(days=DIAS_SEMANA_LAB.index(novo_dia))

        conflito_lab = AgendamentoLab.objects.filter(
            laboratorio=agendamento.laboratorio, data=nova_data, horario=novo_horario, turno=agendamento.turno,
        ).exclude(pk=agendamento.pk).exists()
        conflito_professor = AgendamentoLab.objects.filter(
            professor=agendamento.professor, data=nova_data, horario=novo_horario, turno=agendamento.turno,
        ).exclude(pk=agendamento.pk).exists()

        if conflito_lab:
            messages.error(request, f'{agendamento.laboratorio.nome} ja esta reservado neste novo horario.')
            return redirect('verificar_horario_pdf_confirmar', divergencia_id=divergencia.id)
        if conflito_professor:
            messages.error(request, 'Este professor ja possui outro agendamento neste novo horario.')
            return redirect('verificar_horario_pdf_confirmar', divergencia_id=divergencia.id)

        with transaction.atomic():
            turma_anterior_nome = agendamento.turma.nome if agendamento.turma_id else ''
            dia_anterior = divergencia.dia_semana
            horario_anterior = divergencia.horario

            agendamento.turma = turma_nova
            agendamento.data = nova_data
            agendamento.horario = novo_horario
            agendamento.save()

            HistoricoAlteracaoCronogramaLab.objects.create(
                usuario=request.user if request.user.is_authenticated else None,
                upload_pdf=divergencia.upload,
                laboratorio_nome=agendamento.laboratorio.nome,
                professor_nome=agendamento.professor.nome_completo if agendamento.professor_id else '',
                turno=agendamento.turno,
                turma_nome_anterior=turma_anterior_nome,
                turma_nome_novo=turma_nova.nome,
                dia_anterior=dia_anterior,
                horario_anterior=horario_anterior,
                dia_novo=novo_dia,
                horario_novo=novo_horario,
                disciplina_pdf=divergencia.disciplina_pdf_texto,
            )

            divergencia.decisao = 'alterado'
            divergencia.decidido_por = request.user
            divergencia.decidido_em = timezone.now()
            divergencia.save()

        messages.success(request, 'Reserva do laboratorio atualizada com sucesso.')
        return redirect('verificar_horario_pdf_relatorio', upload_id=divergencia.upload_id)

    context = {
        'divergencia': divergencia,
        'agendamento': agendamento,
        'turmas': Turma.objects.filter(ativa=True).order_by('nome'),
        'dias_semana': DIAS_SEMANA_LAB,
        'horarios': HORARIOS_LAB,
        'turma_sugerida_id': turma_sugerida_id,
        'dia_sugerido': dia_sugerido,
        'horario_sugerido': horario_sugerido,
    }
    return render(request, 'laboratorios/verificar_horario_pdf_confirmar.html', context)


@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def verificar_horario_pdf_aliases(request):
    """Tela de vinculos pendentes entre o texto do PDF e os cadastros de Professor/Turma."""
    if request.method == 'POST':
        tipo = request.POST.get('tipo')
        alias_id = request.POST.get('alias_id')
        acao = request.POST.get('acao')

        if tipo == 'professor':
            alias = get_object_or_404(ProfessorPdfAlias, pk=alias_id)
            if acao == 'ignorar':
                alias.ignorar = True
                alias.professor = None
            else:
                professor_id = request.POST.get('professor_id')
                alias.professor = get_object_or_404(Professor, pk=professor_id) if professor_id else None
                alias.ignorar = False
            alias.atualizado_por = request.user
            alias.save()
            messages.success(request, f'Vinculo de "{alias.texto_pdf}" atualizado.')
        elif tipo == 'turma':
            alias = get_object_or_404(TurmaPdfAlias, pk=alias_id)
            turma_id = request.POST.get('turma_id')
            alias.turma = get_object_or_404(Turma, pk=turma_id) if turma_id else None
            alias.atualizado_por = request.user
            alias.save()
            messages.success(request, f'Vinculo de "{alias.texto_pdf}" atualizado.')

        return redirect('verificar_horario_pdf_aliases')

    professores_pendentes = [
        {'alias': alias, 'sugestao': sugerir_professor(alias.texto_pdf)}
        for alias in ProfessorPdfAlias.objects.filter(professor__isnull=True, ignorar=False).order_by('texto_pdf')
    ]
    turmas_pendentes = [
        {'alias': alias, 'sugestao': sugerir_turma(alias.texto_pdf)}
        for alias in TurmaPdfAlias.objects.filter(turma__isnull=True).order_by('texto_pdf')
    ]

    context = {
        'professores_pendentes': professores_pendentes,
        'turmas_pendentes': turmas_pendentes,
        'professores': Professor.objects.filter(ativo=True).order_by('nome_completo'),
        'turmas': Turma.objects.filter(ativa=True).order_by('nome'),
    }
    return render(request, 'laboratorios/verificar_horario_pdf_aliases.html', context)
