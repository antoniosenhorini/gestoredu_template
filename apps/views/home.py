from .utilitarios import *

SENHA_PADRAO_SEGURANCA = 'zanchim2026'


def _deve_exibir_aviso_senha_padrao(user):
    if not user.is_authenticated or user.is_superuser:
        return False

    usuario_professor = is_professor(user) or Professor.objects.filter(usuario=user, ativo=True).exists()
    usuario_equipe = is_equipe_diretiva(user)
    if not (usuario_professor or usuario_equipe):
        return False

    return user.check_password(SENHA_PADRAO_SEGURANCA)


def home(request):
    eventos = Evento.objects.all().order_by('data')[:5]
    recados = Recado.objects.filter(fixado=True)
    documentos = Documento.objects.all().order_by('-criado_em')  # ← ALTERADO
    videos = Video.objects.all().order_by('-criado_em')[:3]

    return render(request, 'home.html', {
        'eventos': eventos,
        'recados': recados,
        'documentos': documentos,
        'videos': videos,
        'user': request.user,
        'mostrar_aviso_senha_padrao': _deve_exibir_aviso_senha_padrao(request.user),
    })

import json
import unicodedata

from calendar import monthrange
from datetime import date, datetime
from ..models import Aluno, AvisoProfessor, RegistroOcorrenciaAluno, RegistroFaltaAluno  # se ainda não estiver importado
from ..forms import RelatorioFaltasForm
from ..forms import (
    RelatorioFaltasForm, RegistroOcorrenciaForm,
    TIPOS_OCORRENCIA_CHOICES_PERMITIDOS, TIPOS_OCORRENCIA_LEGADOS_FALTA,
)
from ..media_utils import garantir_pastas_media


def _int_param(request, nome, padrao, minimo=None, maximo=None):
    try:
        valor = int(request.GET.get(nome, padrao))
    except (TypeError, ValueError):
        return padrao
    if minimo is not None and valor < minimo:
        return padrao
    if maximo is not None and valor > maximo:
        return padrao
    return valor


def _normalizar_turno(valor):
    if not valor:
        return ''
    texto = unicodedata.normalize('NFKD', str(valor).strip())
    texto = ''.join(char for char in texto if not unicodedata.combining(char)).lower()
    if texto in {'manha', 'matutino'}:
        return 'manha'
    if texto in {'tarde', 'vespertino'}:
        return 'tarde'
    return texto

@painel_required
def painel_equipe(request):
    # --- Conteúdos existentes ---
    documentos_privados = DocumentoPrivado.objects.all().order_by('-criado_em')
    recados_internos = RecadoInterno.objects.all().order_by('-criado_em')[:20]
    eventos_privados = EventoPrivado.objects.all().order_by('data_inicio')
    turmas = Turma.objects.all().order_by('nome')
    avisos_professores_pendentes = AvisoProfessor.objects.filter(visualizado=False).count()

     # ===== GRÁFICO DE FALTAS POR TURNO =====
    hoje = datetime.now()
    mes = _int_param(request, 'mes', hoje.month, 1, 12)
    ano = _int_param(request, 'ano', hoje.year, 2024, 2035)
    dia_raw = request.GET.get('dia', '')

    # Total de alunos por turno
    total_manha = 0
    total_tarde = 0
    for turno in Aluno.objects.filter(ativo=True).values_list('turma__turno', flat=True):
        turno_normalizado = _normalizar_turno(turno)
        if turno_normalizado == 'manha':
            total_manha += 1
        elif turno_normalizado == 'tarde':
            total_tarde += 1

    # Evitar divisão por zero
    # Número de dias no mês
    _, num_dias = monthrange(ano, mes)
    dia_filtro = int(dia_raw) if dia_raw.isdigit() and 1 <= int(dia_raw) <= num_dias else None
    dias_periodo = [dia_filtro] if dia_filtro else list(range(1, num_dias + 1))
    data_inicio = date(ano, mes, dia_filtro or 1)
    data_fim = data_inicio if dia_filtro else date(ano, mes, num_dias)

    # Inicializar contadores por dia
    faltas_manha_por_dia = {d: 0 for d in dias_periodo}
    faltas_tarde_por_dia = {d: 0 for d in dias_periodo}

    # Consulta faltas reais no periodo selecionado, por dia e turno da turma.
    faltas_periodo = RegistroFaltaAluno.objects.filter(
        data__range=(data_inicio, data_fim)
    ).select_related('aluno__turma')

    for falta in faltas_periodo:
        dia_falta = falta.data.day
        if dia_falta not in faltas_manha_por_dia or not falta.aluno_id or not falta.aluno.turma_id:
            continue
        turno = _normalizar_turno(falta.aluno.turma.turno)
        quantidade = falta.quantidade_faltas or 1
        if turno == 'manha':
            faltas_manha_por_dia[dia_falta] += quantidade
        elif turno == 'tarde':
            faltas_tarde_por_dia[dia_falta] += quantidade

    total_faltas_grafico = sum(faltas_manha_por_dia.values()) + sum(faltas_tarde_por_dia.values())
    total_justificativas_periodo = faltas_periodo.filter(justificada=True).count()
    ocorrencias_periodo = RegistroOcorrenciaAluno.objects.filter(
        data__range=(data_inicio, data_fim)
    ).exclude(
        tipo_ocorrencia__in=TIPOS_OCORRENCIA_LEGADOS_FALTA
    ).select_related('aluno__turma')
    total_ocorrencias_periodo = ocorrencias_periodo.count()
    total_alunos_com_ocorrencias = ocorrencias_periodo.values('aluno_id').distinct().count()

    ocorrencias_por_dia = {d: 0 for d in dias_periodo}
    ocorrencias_turno_manha = 0
    ocorrencias_turno_tarde = 0
    for ocorrencia in ocorrencias_periodo:
        dia_ocorrencia = ocorrencia.data.day
        if dia_ocorrencia in ocorrencias_por_dia:
            ocorrencias_por_dia[dia_ocorrencia] += 1
        turno = _normalizar_turno(
            getattr(getattr(getattr(ocorrencia, 'aluno', None), 'turma', None), 'turno', '') or ocorrencia.turno
        )
        if turno == 'manha':
            ocorrencias_turno_manha += 1
        elif turno == 'tarde':
            ocorrencias_turno_tarde += 1

    ocorrencias_por_pedagoga = list(
        ocorrencias_periodo.exclude(atendido_por='')
        .values('atendido_por')
        .annotate(total=Count('id'))
        .order_by('-total', 'atendido_por')
    )
    pedagoga_top_ocorrencias = ocorrencias_por_pedagoga[0] if ocorrencias_por_pedagoga else None
    tipos_ocorrencia_map = dict(TIPOS_OCORRENCIA_CHOICES_PERMITIDOS)
    ranking_tipos_ocorrencia = [
        {
            'tipo': tipos_ocorrencia_map.get(item['tipo_ocorrencia'], item['tipo_ocorrencia'] or 'Nao informado'),
            'total': item['total'],
        }
        for item in ocorrencias_periodo.values('tipo_ocorrencia')
        .annotate(total=Count('id'))
        .order_by('-total', 'tipo_ocorrencia')
    ]

    # Calcula percentuais para cada dia
    percentuais_manha = []
    percentuais_tarde = []
    for dia_num in dias_periodo:
        perc_manha = (faltas_manha_por_dia[dia_num] / max(total_manha, 1)) * 100
        perc_tarde = (faltas_tarde_por_dia[dia_num] / max(total_tarde, 1)) * 100
        percentuais_manha.append(round(perc_manha, 2))
        percentuais_tarde.append(round(perc_tarde, 2))

    dias = dias_periodo
    # ===== FIM DO GRÁFICO =====

    # ===== RANKING DE FALTAS POR TURMA =====
    # Captura mês e ano do ranking (GET) ou usa o mês/ano atual
    mes_ranking = _int_param(request, 'mes_ranking', hoje.month, 1, 12)
    ano_ranking = _int_param(request, 'ano_ranking', hoje.year, 2024, 2035)
    dia_ranking_raw = request.GET.get('dia_ranking', '')
    turno_ranking = _normalizar_turno(request.GET.get('turno_ranking', ''))
    if turno_ranking not in {'', 'manha', 'tarde'}:
        turno_ranking = ''
    _, num_dias_ranking = monthrange(ano_ranking, mes_ranking)
    dia_ranking = int(dia_ranking_raw) if dia_ranking_raw.isdigit() and 1 <= int(dia_ranking_raw) <= num_dias_ranking else None

    # Busca faltas no período
    faltas_ranking = RegistroFaltaAluno.objects.filter(
        data__year=ano_ranking,
        data__month=mes_ranking
    ).select_related('aluno__turma')
    if dia_ranking:
        faltas_ranking = faltas_ranking.filter(data__day=dia_ranking)

    # Conta faltas por turma e separa por turno
    turmas_manha = {}
    turmas_tarde = {}

    for falta in faltas_ranking:
        if not falta.aluno_id or not falta.aluno.turma_id:
            continue
        turma_nome = falta.aluno.turma.nome
        turno = _normalizar_turno(falta.aluno.turma.turno)
        if turno_ranking and turno != turno_ranking:
            continue
        quantidade = falta.quantidade_faltas or 1
        if turno == 'manha':
            turmas_manha[turma_nome] = turmas_manha.get(turma_nome, 0) + quantidade
        elif turno == 'tarde':
            turmas_tarde[turma_nome] = turmas_tarde.get(turma_nome, 0) + quantidade

    # Ordena por total de faltas (decrescente)
    ranking_manha = sorted(turmas_manha.items(), key=lambda x: x[1], reverse=True)
    ranking_tarde = sorted(turmas_tarde.items(), key=lambda x: x[1], reverse=True)

    # Separa dados para os gráficos
    turmas_manha_lista = [item[0] for item in ranking_manha]
    totais_manha_lista = [item[1] for item in ranking_manha]
    turmas_tarde_lista = [item[0] for item in ranking_tarde]
    totais_tarde_lista = [item[1] for item in ranking_tarde]
    # ===== FIM DO RANKING =====

    # ===== RANKING DE OCORRENCIAS POR TURMA =====
    mes_ocorrencias_ranking = _int_param(request, 'mes_ocorrencias_ranking', mes, 1, 12)
    ano_ocorrencias_ranking = _int_param(request, 'ano_ocorrencias_ranking', ano, 2024, 2035)
    dia_ocorrencias_ranking_raw = request.GET.get('dia_ocorrencias_ranking', '')
    turno_ocorrencias_ranking = _normalizar_turno(request.GET.get('turno_ocorrencias_ranking', ''))
    if turno_ocorrencias_ranking not in {'', 'manha', 'tarde'}:
        turno_ocorrencias_ranking = ''
    _, num_dias_ocorrencias_ranking = monthrange(ano_ocorrencias_ranking, mes_ocorrencias_ranking)
    dia_ocorrencias_ranking = (
        int(dia_ocorrencias_ranking_raw)
        if dia_ocorrencias_ranking_raw.isdigit() and 1 <= int(dia_ocorrencias_ranking_raw) <= num_dias_ocorrencias_ranking
        else None
    )

    ocorrencias_ranking = RegistroOcorrenciaAluno.objects.filter(
        data__year=ano_ocorrencias_ranking,
        data__month=mes_ocorrencias_ranking,
    ).exclude(
        tipo_ocorrencia__in=TIPOS_OCORRENCIA_LEGADOS_FALTA
    ).select_related('aluno__turma')
    if dia_ocorrencias_ranking:
        ocorrencias_ranking = ocorrencias_ranking.filter(data__day=dia_ocorrencias_ranking)

    ocorrencias_turmas_manha = {}
    ocorrencias_turmas_tarde = {}
    for ocorrencia in ocorrencias_ranking:
        if not ocorrencia.aluno_id or not ocorrencia.aluno.turma_id:
            continue
        turno = _normalizar_turno(ocorrencia.aluno.turma.turno or ocorrencia.turno)
        if turno_ocorrencias_ranking and turno != turno_ocorrencias_ranking:
            continue
        turma_nome = f'{ocorrencia.aluno.turma.serie} - {ocorrencia.aluno.turma.nome}'
        if turno == 'manha':
            ocorrencias_turmas_manha[turma_nome] = ocorrencias_turmas_manha.get(turma_nome, 0) + 1
        elif turno == 'tarde':
            ocorrencias_turmas_tarde[turma_nome] = ocorrencias_turmas_tarde.get(turma_nome, 0) + 1

    ranking_ocorrencias_manha = sorted(ocorrencias_turmas_manha.items(), key=lambda x: x[1], reverse=True)
    ranking_ocorrencias_tarde = sorted(ocorrencias_turmas_tarde.items(), key=lambda x: x[1], reverse=True)
    ocorrencias_turmas_manha_lista = [item[0] for item in ranking_ocorrencias_manha]
    ocorrencias_totais_manha_lista = [item[1] for item in ranking_ocorrencias_manha]
    ocorrencias_turmas_tarde_lista = [item[0] for item in ranking_ocorrencias_tarde]
    ocorrencias_totais_tarde_lista = [item[1] for item in ranking_ocorrencias_tarde]
    # ===== FIM DO RANKING DE OCORRENCIAS =====

    # ===== CONTEXTO =====
    context = {
        'usuario': request.user,
        'mostrar_aviso_senha_padrao': _deve_exibir_aviso_senha_padrao(request.user),
        'documentos_privados': documentos_privados,
        'recados_internos': recados_internos,
        'eventos_privados': eventos_privados,
        'turmas': turmas,
        'avisos_professores_pendentes': avisos_professores_pendentes,
        'mes_atual': datetime.now().month,
        'ano_atual': datetime.now().year,
        # Dados para o gráfico de faltas diárias
        'dias': dias,
        'percentuais_manha': percentuais_manha,
        'percentuais_tarde': percentuais_tarde,
        'total_manha': total_manha,
        'total_tarde': total_tarde,
        'faltas_manha_por_dia': list(faltas_manha_por_dia.values()),
        'faltas_tarde_por_dia': list(faltas_tarde_por_dia.values()),
        'total_faltas_grafico': total_faltas_grafico,
        'total_ocorrencias_periodo': total_ocorrencias_periodo,
        'total_alunos_com_ocorrencias': total_alunos_com_ocorrencias,
        'ocorrencias_por_dia': list(ocorrencias_por_dia.values()),
        'ocorrencias_turno_manha': ocorrencias_turno_manha,
        'ocorrencias_turno_tarde': ocorrencias_turno_tarde,
        'ocorrencias_por_pedagoga': ocorrencias_por_pedagoga,
        'pedagoga_top_ocorrencias': pedagoga_top_ocorrencias,
        'ranking_tipos_ocorrencia': ranking_tipos_ocorrencia,
        'total_justificativas_periodo': total_justificativas_periodo,
        'mes': mes,
        'ano': ano,
        'dia': dia_filtro,
        'dias_filtro': range(1, 32),
        'meses': range(1, 13),
        'anos': range(2024, 2027),
        # Dados para o ranking
        'ranking_manha': ranking_manha,
        'ranking_tarde': ranking_tarde,
        'turmas_manha': turmas_manha_lista,
        'totais_manha': totais_manha_lista,
        'turmas_tarde': turmas_tarde_lista,
        'totais_tarde': totais_tarde_lista,
        'turno_ranking': turno_ranking,
        'mes_ranking': mes_ranking,
        'ano_ranking': ano_ranking,
        'dia_ranking': dia_ranking,
        'total_faltas_periodo': sum(totais_manha_lista) + sum(totais_tarde_lista),
        'sem_dados_faltas_diarias': not any(faltas_manha_por_dia.values()) and not any(faltas_tarde_por_dia.values()),
        'sem_dados_ranking': not turmas_manha_lista and not turmas_tarde_lista,
        'ranking_ocorrencias_manha': ranking_ocorrencias_manha,
        'ranking_ocorrencias_tarde': ranking_ocorrencias_tarde,
        'ocorrencias_turmas_manha': ocorrencias_turmas_manha_lista,
        'ocorrencias_totais_manha': ocorrencias_totais_manha_lista,
        'ocorrencias_turmas_tarde': ocorrencias_turmas_tarde_lista,
        'ocorrencias_totais_tarde': ocorrencias_totais_tarde_lista,
        'turno_ocorrencias_ranking': turno_ocorrencias_ranking,
        'mes_ocorrencias_ranking': mes_ocorrencias_ranking,
        'ano_ocorrencias_ranking': ano_ocorrencias_ranking,
        'dia_ocorrencias_ranking': dia_ocorrencias_ranking,
        'sem_dados_ocorrencias_diarias': not any(ocorrencias_por_dia.values()),
        'sem_dados_ranking_ocorrencias': not ocorrencias_turmas_manha_lista and not ocorrencias_turmas_tarde_lista,
        'sem_dados_tipos_ocorrencia': not ranking_tipos_ocorrencia,
    }
    for chave in (
        'dias', 'percentuais_manha', 'percentuais_tarde',
        'turmas_manha', 'totais_manha', 'turmas_tarde', 'totais_tarde',
        'ocorrencias_por_dia', 'ocorrencias_turmas_manha', 'ocorrencias_totais_manha',
        'ocorrencias_turmas_tarde', 'ocorrencias_totais_tarde'
    ):
        context[f'{chave}_json'] = json.dumps(context[chave], ensure_ascii=False)
    context['ranking_tipos_ocorrencia_labels_json'] = json.dumps(
        [item['tipo'] for item in ranking_tipos_ocorrencia],
        ensure_ascii=False,
    )
    context['ranking_tipos_ocorrencia_totais_json'] = json.dumps(
        [item['total'] for item in ranking_tipos_ocorrencia],
        ensure_ascii=False,
    )
    return render(request, 'painel_equipe.html', context)

@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def upload_documento_privado(request):
    if request.method == 'POST':
        titulo = request.POST.get('titulo')
        categoria = request.POST.get('categoria')
        descricao = request.POST.get('descricao')
        arquivo = request.FILES.get('arquivo')
        if titulo and arquivo:
            garantir_pastas_media()
            DocumentoPrivado.objects.create(
                titulo=titulo,
                arquivo=arquivo,
                categoria=categoria,
                descricao=descricao,
                criado_por=request.user
            )
    return redirect('painel_equipe')



@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def criar_evento_privado(request):
    if request.method == 'POST':
        titulo = request.POST.get('titulo')
        descricao = request.POST.get('descricao')
        data_inicio = request.POST.get('data_inicio')
        data_fim = request.POST.get('data_fim')
        local = request.POST.get('local')
        if titulo and data_inicio:
            EventoPrivado.objects.create(
                titulo=titulo,
                descricao=descricao,
                data_inicio=data_inicio,
                data_fim=data_fim if data_fim else None,
                local=local,
                criado_por=request.user
            )
    return redirect('painel_equipe')

@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def enviar_recado_interno(request):
    if request.method == 'POST':
        mensagem = request.POST.get('mensagem')
        arquivo = request.FILES.get('arquivo')
        if mensagem:
            garantir_pastas_media()
            RecadoInterno.objects.create(
                mensagem=mensagem,
                arquivo=arquivo,
                criado_por=request.user
            )
    return redirect('painel_equipe')

def detalhe_evento(request, evento_id):
    from ..models import Evento
    evento = Evento.objects.get(id=evento_id)
    return render(request, 'detalhe_evento.html', {'evento': evento})

def detalhe_recado(request, recado_id):
    from ..models import Recado
    recado = Recado.objects.get(id=recado_id)
    return render(request, 'detalhe_recado.html', {'recado': recado})

# =============================================================================
# NOVAS VIEWS PARA TELAS CUSTOMIZADAS
# =============================================================================

@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def lista_recados_internos(request):
    if request.method == 'POST':
        garantir_pastas_media()
        form = RecadoInternoForm(request.POST, request.FILES)
        if form.is_valid():
            recado = form.save(commit=False)
            recado.criado_por = request.user
            recado.save()
            return redirect('lista_recados_internos')
    else:
        form = RecadoInternoForm()

    recados = RecadoInterno.objects.all().order_by('-criado_em')
    return render(request, 'lista_recados_internos.html', {
        'recados': recados,
        'form': form
    })

@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def lista_documentos_privados(request):
    if request.method == 'POST':
        garantir_pastas_media()
        form = DocumentoPrivadoForm(request.POST, request.FILES)
        if form.is_valid():
            doc = form.save(commit=False)
            doc.criado_por = request.user
            doc.save()
            return redirect('lista_documentos_privados')
    else:
        form = DocumentoPrivadoForm()

    documentos = DocumentoPrivado.objects.all().order_by('-criado_em')
    return render(request, 'lista_documentos_privados.html', {
        'documentos': documentos,
        'form': form
    })

@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def lista_eventos_privados(request):
    if request.method == 'POST':
        form = EventoPrivadoForm(request.POST)
        if form.is_valid():
            evento = form.save(commit=False)
            evento.criado_por = request.user
            evento.save()
            return redirect('lista_eventos_privados')
    else:
        form = EventoPrivadoForm()

    eventos = EventoPrivado.objects.all().order_by('-data_inicio')
    return render(request, 'lista_eventos_privados.html', {
        'eventos': eventos,
        'form': form
    })
