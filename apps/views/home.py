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

    # ===== GRÁFICO DE FALTAS (INTEGRAL) =====
    hoje = datetime.now()
    mes = _int_param(request, 'mes', hoje.month, 1, 12)
    ano = _int_param(request, 'ano', hoje.year, 2024, 2035)
    dia_raw = request.GET.get('dia', '')

    # Total de alunos ativos (Integral)
    total_alunos = Aluno.objects.filter(ativo=True).count()

    _, num_dias = monthrange(ano, mes)
    dia_filtro = int(dia_raw) if dia_raw.isdigit() and 1 <= int(dia_raw) <= num_dias else None
    dias_periodo = [dia_filtro] if dia_filtro else list(range(1, num_dias + 1))
    data_inicio = date(ano, mes, dia_filtro or 1)
    data_fim = data_inicio if dia_filtro else date(ano, mes, num_dias)

    faltas_integral_por_dia = {d: 0 for d in dias_periodo}

    faltas_periodo = RegistroFaltaAluno.objects.filter(
        data__range=(data_inicio, data_fim)
    ).select_related('aluno__turma')

    for falta in faltas_periodo:
        dia_falta = falta.data.day
        if dia_falta in faltas_integral_por_dia:
            faltas_integral_por_dia[dia_falta] += (falta.quantidade_faltas or 1)

    total_faltas_grafico = sum(faltas_integral_por_dia.values())
    total_justificativas_periodo = faltas_periodo.filter(justificada=True).count()
    
    ocorrencias_periodo = RegistroOcorrenciaAluno.objects.filter(
        data__range=(data_inicio, data_fim)
    ).exclude(
        tipo_ocorrencia__in=TIPOS_OCORRENCIA_LEGADOS_FALTA
    ).select_related('aluno__turma')
    total_ocorrencias_periodo = ocorrencias_periodo.count()
    total_alunos_com_ocorrencias = ocorrencias_periodo.values('aluno_id').distinct().count()

    ocorrencias_por_dia = {d: 0 for d in dias_periodo}
    for ocorrencia in ocorrencias_periodo:
        dia_ocorrencia = ocorrencia.data.day
        if dia_ocorrencia in ocorrencias_por_dia:
            ocorrencias_por_dia[dia_ocorrencia] += 1

    # Corrigido: Ignora quem está em branco ou só com espaços
    ocorrencias_por_pedagoga = list(
        ocorrencias_periodo.exclude(atendido_por__isnull=True)
        .exclude(atendido_por__regex=r'^\s*$')
        .exclude(atendido_por__iexact='Nao definida')   # <-- ADICIONE
        .exclude(atendido_por__iexact='Não definida')   # <-- ADICIONE
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

    percentuais_integral = []
    for dia_num in dias_periodo:
        perc = (faltas_integral_por_dia[dia_num] / max(total_alunos, 1)) * 100
        percentuais_integral.append(round(perc, 2))

    dias = dias_periodo
    # ===== FIM DO GRÁFICO =====

    # ===== RANKING DE FALTAS POR TURMA (INTEGRAL) =====
    mes_ranking = _int_param(request, 'mes_ranking', hoje.month, 1, 12)
    ano_ranking = _int_param(request, 'ano_ranking', hoje.year, 2024, 2035)
    dia_ranking_raw = request.GET.get('dia_ranking', '')
    _, num_dias_ranking = monthrange(ano_ranking, mes_ranking)
    dia_ranking = int(dia_ranking_raw) if dia_ranking_raw.isdigit() and 1 <= int(dia_ranking_raw) <= num_dias_ranking else None

    faltas_ranking = RegistroFaltaAluno.objects.filter(
        data__year=ano_ranking,
        data__month=mes_ranking
    ).select_related('aluno__turma')
    if dia_ranking:
        faltas_ranking = faltas_ranking.filter(data__day=dia_ranking)

    turmas_integral = {}
    for falta in faltas_ranking:
        if not falta.aluno_id or not falta.aluno.turma_id:
            continue
        turma_nome = falta.aluno.turma.nome
        turmas_integral[turma_nome] = turmas_integral.get(turma_nome, 0) + (falta.quantidade_faltas or 1)

    ranking_integral = sorted(turmas_integral.items(), key=lambda x: x[1], reverse=True)
    turmas_integral_lista = [item[0] for item in ranking_integral]
    totais_integral_lista = [item[1] for item in ranking_integral]
    # ===== FIM DO RANKING =====

    # ===== RANKING DE OCORRENCIAS POR TURMA (INTEGRAL) =====
    mes_ocorrencias_ranking = _int_param(request, 'mes_ocorrencias_ranking', mes, 1, 12)
    ano_ocorrencias_ranking = _int_param(request, 'ano_ocorrencias_ranking', ano, 2024, 2035)
    dia_ocorrencias_ranking_raw = request.GET.get('dia_ocorrencias_ranking', '')
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

    ocorrencias_turmas_integral = {}
    for ocorrencia in ocorrencias_ranking:
        if not ocorrencia.aluno_id or not ocorrencia.aluno.turma_id:
            continue
        turma_nome = str(ocorrencia.aluno.turma) # <-- Vai puxar o nome + curso bonitinho
        ocorrencias_turmas_integral[turma_nome] = ocorrencias_turmas_integral.get(turma_nome, 0) + 1

    ranking_ocorrencias_integral = sorted(ocorrencias_turmas_integral.items(), key=lambda x: x[1], reverse=True)
    ocorrencias_turmas_integral_lista = [item[0] for item in ranking_ocorrencias_integral]
    ocorrencias_totais_integral_lista = [item[1] for item in ranking_ocorrencias_integral]
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
        
        # Dados para o gráfico de faltas diárias (Integral)
        'dias': dias,
        'percentuais_integral': percentuais_integral,
        'total_alunos': total_alunos,
        'faltas_integral_por_dia': list(faltas_integral_por_dia.values()),
        'total_faltas_grafico': total_faltas_grafico,
        'total_ocorrencias_periodo': total_ocorrencias_periodo,
        'total_alunos_com_ocorrencias': total_alunos_com_ocorrencias,
        'ocorrencias_por_dia': list(ocorrencias_por_dia.values()),
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
        
        # Dados para o ranking (Integral)
        'ranking_integral': ranking_integral,
        'turmas_integral': turmas_integral_lista,
        'totais_integral': totais_integral_lista,
        'mes_ranking': mes_ranking,
        'ano_ranking': ano_ranking,
        'dia_ranking': dia_ranking,
        'total_faltas_periodo': sum(totais_integral_lista),
        'sem_dados_faltas_diarias': not any(faltas_integral_por_dia.values()),
        'sem_dados_ranking': not turmas_integral_lista,
        
        # Ranking de ocorrências (Integral)
        'ranking_ocorrencias_integral': ranking_ocorrencias_integral,
        'ocorrencias_turmas_integral': ocorrencias_turmas_integral_lista,
        'ocorrencias_totais_integral': ocorrencias_totais_integral_lista,
        'mes_ocorrencias_ranking': mes_ocorrencias_ranking,
        'ano_ocorrencias_ranking': ano_ocorrencias_ranking,
        'dia_ocorrencias_ranking': dia_ocorrencias_ranking,
        'sem_dados_ocorrencias_diarias': not any(ocorrencias_por_dia.values()),
        'sem_dados_ranking_ocorrencias': not ocorrencias_turmas_integral_lista,
        'sem_dados_tipos_ocorrencia': not ranking_tipos_ocorrencia,
    }
    
    for chave in (
        'dias', 'percentuais_integral',
        'turmas_integral', 'totais_integral',
        'ocorrencias_por_dia', 'ocorrencias_turmas_integral', 'ocorrencias_totais_integral'
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
