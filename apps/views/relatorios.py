# -*- coding: utf-8 -*-
from .utilitarios import *
from ..forms import RelatorioFaltasForm
from django.db.models import Sum, Q

# ===== NOVA VIEW: RELATÓRIO DE FALTAS POR ALUNO =====
@relatorios_required
def relatorio_faltas(request):
    form = RelatorioFaltasForm()
    erro = None

    if request.method == 'POST':
        form = RelatorioFaltasForm(request.POST)
        if form.is_valid():
            turma = form.cleaned_data['turma']
            identificador = form.cleaned_data['identificador']

            # Buscar aluno
            aluno = None
            if identificador.isdigit():
                aluno = Aluno.objects.filter(turma=turma, numero=int(identificador)).first()
            else:
                aluno = Aluno.objects.filter(turma=turma, nome__icontains=identificador).first()

            if not aluno:
                erro = 'Aluno não encontrado nesta turma.'
            else:
                # Busca faltas reais de alunos; ocorrencias disciplinares ficam em outro model.
                faltas = RegistroFaltaAluno.objects.filter(aluno=aluno).order_by('-data')
                total_faltas = sum(falta.quantidade_faltas or 1 for falta in faltas)
                return render(request, 'faltas/relatorio_faltas.html', {
                    'aluno': aluno,
                    'faltas': faltas,
                    'total_faltas': total_faltas
                })

    return render(request, 'faltas/form_relatorio_faltas.html', {'form': form, 'erro': erro})

@relatorios_required
def relatorio_faltas_por_aluno(request):
    # Pega mês e ano da URL (ou atual)
    mes = int(request.GET.get('mes', timezone.now().month))
    ano = int(request.GET.get('ano', timezone.now().year))

    # Filtra faltas reais de alunos no periodo.
    faltas = RegistroFaltaAluno.objects.filter(
        data__year=ano,
        data__month=mes
    ).select_related('aluno', 'aluno__turma')

    # Agrupa por aluno e conta faltas
    alunos_faltas = {}
    for falta in faltas:
        aluno = falta.aluno
        key = (aluno.turma.nome, aluno.numero, aluno.nome)
        alunos_faltas[key] = alunos_faltas.get(key, 0) + (falta.quantidade_faltas or 1)

    # Converte para lista ordenada
    dados = []
    for (turma, numero, nome), total in sorted(alunos_faltas.items(), key=lambda x: (x[0][0], x[0][1])):
        dados.append({
            'turma': turma,
            'numero': numero,
            'nome': nome,
            'faltas': total,
        })

    context = {
        'dados': dados,
        'mes': mes,
        'ano': ano,
        'meses': range(1, 13),
        'anos': range(2024, 2028),
    }
    return render(request, 'faltas/relatorio_faltas_por_aluno.html', context)


@relatorios_required
def relatorio_alunos_mais_faltosos(request):
    turnos_validos = ['manha', 'tarde', 'integral']
    turno_filtro = request.GET.get('turno', 'integral')
    if turno_filtro not in turnos_validos:
        turno_filtro = 'integral'

    turma_filtro = request.GET.get('turma', '')
    data_inicial = request.GET.get('data_inicial', '')
    data_final = request.GET.get('data_final', '')
    data_inicial_obj = None
    data_final_obj = None

    if turma_filtro and not turma_filtro.isdigit():
        turma_filtro = ''

    if data_inicial:
        try:
            data_inicial_obj = datetime.strptime(data_inicial, '%Y-%m-%d').date()
        except ValueError:
            data_inicial = ''

    if data_final:
        try:
            data_final_obj = datetime.strptime(data_final, '%Y-%m-%d').date()
        except ValueError:
            data_final = ''

    if turno_filtro == 'integral':
        turmas = Turma.objects.filter(Q(turno='integral') | Q(turno=None) | Q(turno=''), ativa=True).order_by('nome')
        faltas = RegistroFaltaAluno.objects.filter(
            Q(aluno__turma__turno='integral') | Q(aluno__turma__turno=None) | Q(aluno__turma__turno='')
        ).select_related('aluno', 'aluno__turma')
    else:
        turmas = Turma.objects.filter(ativa=True, turno=turno_filtro).order_by('nome')
        faltas = RegistroFaltaAluno.objects.filter(
            aluno__turma__turno=turno_filtro
        ).select_related('aluno', 'aluno__turma')

    if turma_filtro:
        faltas = faltas.filter(aluno__turma_id=turma_filtro)
    if data_inicial_obj:
        faltas = faltas.filter(data__gte=data_inicial_obj)
    if data_final_obj:
        faltas = faltas.filter(data__lte=data_final_obj)

    totais = (
        faltas
        .values(
            'aluno_id',
            'aluno__nome',
            'aluno__numero',
            'aluno__turma_id',
            'aluno__turma__nome',
            'aluno__turma__serie',
        )
        .annotate(total_registros=Sum('quantidade_faltas'))
        .order_by('aluno__turma__nome', '-total_registros', 'aluno__nome')
    )

    turmas_relatorio = []
    turma_atual_id = None
    grupo_atual = None
    posicao = 0
    total_alunos = 0

    for item in totais:
        total_registros = item['total_registros'] or 0
        if total_registros <= 0:
            continue

        if item['aluno__turma_id'] != turma_atual_id:
            turma_atual_id = item['aluno__turma_id']
            posicao = 0
            grupo_atual = {
                'turma_id': turma_atual_id,
                'turma': item['aluno__turma__nome'],
                'serie': item['aluno__turma__serie'],
                'alunos': [],
            }
            turmas_relatorio.append(grupo_atual)

        posicao += 1
        total_alunos += 1
        grupo_atual['alunos'].append({
            'posicao': posicao,
            'nome': item['aluno__nome'],
            'numero': item['aluno__numero'],
            'turma': item['aluno__turma__nome'],
            'serie': item['aluno__turma__serie'],
            'total_aulas_faltadas': total_registros * 6,
        })

    context = {
        'turno_filtro': turno_filtro,
        'turma_filtro': turma_filtro,
        'data_inicial': data_inicial,
        'data_final': data_final,
        'turmas': turmas,
        'turmas_relatorio': turmas_relatorio,
        'total_alunos': total_alunos,
    }
    return render(request, 'faltas/relatorio_alunos_mais_faltosos.html', context)


@relatorios_required
def relatorio_alunos_mais_ocorrencias(request):
    turnos_validos = ['manha', 'tarde']
    turno_filtro = request.GET.get('turno', 'manha')
    if turno_filtro not in turnos_validos:
        turno_filtro = 'manha'

    turma_filtro = request.GET.get('turma', '')
    data_inicial = request.GET.get('data_inicial', '')
    data_final = request.GET.get('data_final', '')
    data_inicial_obj = None
    data_final_obj = None

    if turma_filtro and not turma_filtro.isdigit():
        turma_filtro = ''

    if data_inicial:
        try:
            data_inicial_obj = datetime.strptime(data_inicial, '%Y-%m-%d').date()
        except ValueError:
            data_inicial = ''

    if data_final:
        try:
            data_final_obj = datetime.strptime(data_final, '%Y-%m-%d').date()
        except ValueError:
            data_final = ''

    turmas = Turma.objects.filter(ativa=True, turno=turno_filtro).order_by('nome')

    ocorrencias = RegistroOcorrenciaAluno.objects.filter(
        aluno__turma__turno=turno_filtro,
        faltou=False,
    ).exclude(
        tipo_ocorrencia__in=TIPOS_OCORRENCIA_LEGADOS_FALTA
    ).select_related('aluno', 'aluno__turma')

    if turma_filtro:
        ocorrencias = ocorrencias.filter(aluno__turma_id=turma_filtro)
    if data_inicial_obj:
        ocorrencias = ocorrencias.filter(data__gte=data_inicial_obj)
    if data_final_obj:
        ocorrencias = ocorrencias.filter(data__lte=data_final_obj)

    tipo_nomes = dict(TIPOS_OCORRENCIA_CHOICES_PERMITIDOS)
    alunos_por_turma = {}

    for ocorrencia in ocorrencias.order_by('aluno__turma__nome', 'aluno__nome'):
        aluno = ocorrencia.aluno
        turma = aluno.turma
        turma_id = turma.id

        if turma_id not in alunos_por_turma:
            alunos_por_turma[turma_id] = {
                'turma_id': turma_id,
                'turma': turma.nome,
                'serie': turma.serie,
                'alunos': {},
            }

        alunos = alunos_por_turma[turma_id]['alunos']
        if aluno.id not in alunos:
            alunos[aluno.id] = {
                'nome': aluno.nome,
                'numero': aluno.numero,
                'turma': turma.nome,
                'serie': turma.serie,
                'total_ocorrencias': 0,
                'tipos': {},
            }

        aluno_item = alunos[aluno.id]
        tipo_codigo = ocorrencia.tipo_ocorrencia or ''
        tipo_nome = tipo_nomes.get(tipo_codigo, tipo_codigo or 'Nao informado')
        aluno_item['total_ocorrencias'] += 1
        aluno_item['tipos'][tipo_nome] = aluno_item['tipos'].get(tipo_nome, 0) + 1

    turmas_relatorio = []
    total_alunos = 0

    for grupo in sorted(alunos_por_turma.values(), key=lambda item: item['turma']):
        alunos_ordenados = sorted(
            grupo['alunos'].values(),
            key=lambda item: (-item['total_ocorrencias'], item['nome'])
        )
        alunos = []
        for posicao, aluno_item in enumerate(alunos_ordenados, start=1):
            tipos = [
                {'nome': nome, 'total': total}
                for nome, total in sorted(aluno_item['tipos'].items(), key=lambda item: (-item[1], item[0]))
            ]
            alunos.append({
                'posicao': posicao,
                'nome': aluno_item['nome'],
                'numero': aluno_item['numero'],
                'turma': aluno_item['turma'],
                'serie': aluno_item['serie'],
                'total_ocorrencias': aluno_item['total_ocorrencias'],
                'tipos': tipos,
            })

        if alunos:
            total_alunos += len(alunos)
            turmas_relatorio.append({
                'turma_id': grupo['turma_id'],
                'turma': grupo['turma'],
                'serie': grupo['serie'],
                'alunos': alunos,
            })

    context = {
        'turno_filtro': turno_filtro,
        'turma_filtro': turma_filtro,
        'data_inicial': data_inicial,
        'data_final': data_final,
        'turmas': turmas,
        'turmas_relatorio': turmas_relatorio,
        'total_alunos': total_alunos,
    }
    return render(request, 'ocorrencias/relatorio_alunos_mais_ocorrencias.html', context)


@relatorios_required
def exportar_relatorio_faltas(request):
    mes = int(request.GET.get('mes', timezone.now().month))
    ano = int(request.GET.get('ano', timezone.now().year))

    faltas = RegistroFaltaAluno.objects.filter(
        data__year=ano,
        data__month=mes
    ).select_related('aluno', 'aluno__turma').order_by('data', 'aluno__turma__nome', 'aluno__numero')

    data = []
    for falta in faltas:
        data.append({
            'Data': falta.data.strftime('%d/%m/%Y'),
            'Turma': falta.aluno.turma.nome,
            'Nº': falta.aluno.numero,
            'Aluno': falta.aluno.nome,
            'Faltas': falta.quantidade_faltas or 1,
            'Justificada': 'Sim' if falta.justificada else 'Não',
            'Pedagoga': falta.pedagoga,
            'Responsável contatado': falta.responsavel_contatado,
            'Observações': falta.observacoes,
        })

    colunas_ordem = ['Data', 'Turma', 'Nº', 'Aluno', 'Faltas', 'Justificada',
                     'Pedagoga', 'Responsável contatado', 'Observações']

    df = pd.DataFrame(data, columns=colunas_ordem)
    if df.empty:
        df = pd.DataFrame(columns=colunas_ordem)

    output = BytesIO()
    sheet_name = f'Faltas_{mes:02d}_{ano}'
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name=sheet_name, index=False)
        worksheet = writer.sheets[sheet_name]

        for column in worksheet.columns:
            max_length = 0
            column_letter = column[0].column_letter
            for cell in column:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except:
                    pass
            adjusted_width = min(max_length + 2, 50)
            worksheet.column_dimensions[column_letter].width = adjusted_width

    output.seek(0)
    response = HttpResponse(output, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="relatorio_faltas_{mes:02d}_{ano}.xlsx"'
    return response

@relatorios_required
def relatorio_faltas_mensal(request):
    mes = request.GET.get('mes', timezone.now().month)
    ano = request.GET.get('ano', timezone.now().year)
    professores = User.objects.filter(groups__name__iexact='Equipe Diretiva')
    relatorio = []
    for professor in professores:
        faltas = RegistroFalta.objects.filter(professor=professor, data__month=mes, data__year=ano).order_by('data', 'periodo__ordem')
        total_minutos = sum(f.minutos_faltados() for f in faltas)
        total_faltas = len([f for f in faltas if f.tipo == 'falta'])
        total_atrasos = len([f for f in faltas if f.tipo == 'atraso'])
        relatorio.append({
            'professor': professor.get_full_name() or professor.username,
            'faltas': faltas,
            'total_minutos': total_minutos,
            'total_horas': round(total_minutos / 60, 1),
            'total_faltas': total_faltas,
            'total_atrasos': total_atrasos,
        })
    context = {
        'relatorio': relatorio,
        'mes': mes,
        'ano': ano,
        'meses': range(1, 13),
        'anos': [2026, 2027],
    }
    return render(request, 'faltas/relatorio_faltas.html', context)

# ===== HISTÓRICO DE ACESSOS =====
@login_required
def historico_acessos(request):
    # Apenas superusuários podem acessar
    if not request.user.is_superuser:
        return redirect('painel_equipe')

    logs = LogLogin.objects.all().order_by('-data_hora')
    return render(request, 'historico_acessos.html', {'logs': logs})

@relatorios_required
def lancar_ponto(request):
    from ..models import PeriodoAula, RegistroPonto
    professores = User.objects.filter(groups__name__iexact='Equipe Diretiva').order_by('first_name')
    periodos = PeriodoAula.objects.all().order_by('ordem')
    data_atual = timezone.now().date()
    if request.method == 'POST':
        professor_id = request.POST.get('professor')
        data = request.POST.get('data')
        periodo_id = request.POST.get('periodo')
        acao = request.POST.get('acao')
        professor = User.objects.get(id=professor_id)
        periodo = PeriodoAula.objects.get(id=periodo_id)
        registro, created = RegistroPonto.objects.get_or_create(
            professor=professor,
            data=data,
            periodo=periodo,
            defaults={
                'horario_previsto_inicio': periodo.inicio,
                'horario_previsto_fim': periodo.fim,
                'registrado_por': request.user,
            }
        )
        if acao == 'entrada':
            registro.entrada_real = timezone.now()
            messages.success(request, f'Entrada registrada para {professor.get_full_name()} às {timezone.now().strftime("%H:%M")}')
        elif acao == 'saida':
            registro.saida_real = timezone.now()
            messages.success(request, f'Saída registrada para {professor.get_full_name()} às {timezone.now().strftime("%H:%M")}' )
        registro.save()
        return redirect('lancar_ponto')
    context = {
        'professores': professores,
        'periodos': periodos,
        'data_atual': data_atual,
        'hoje': data_atual,
    }
    return render(request, 'lancar_ponto.html', context)

@relatorios_required
def relatorio_ponto(request):
    from ..models import RegistroPonto
    mes = request.GET.get('mes', timezone.now().month)
    ano = request.GET.get('ano', timezone.now().year)
    professores = User.objects.filter(groups__name__iexact='Equipe Diretiva')
    registros = RegistroPonto.objects.filter(data__month=mes, data__year=ano).select_related('professor', 'periodo').order_by('data', 'periodo__ordem')
    resumo = []
    for professor in professores:
        registros_prof = [r for r in registros if r.professor.id == professor.id]
        total_horas_trabalhadas = sum(r.horas_trabalhadas() for r in registros_prof)
        total_horas_previstas = sum(r.horas_previstas() for r in registros_prof)
        saldo_total = sum(r.saldo() for r in registros_prof)
        resumo.append({
            'nome': professor.get_full_name() or professor.username,
            'total_horas_trabalhadas': round(total_horas_trabalhadas, 2),
            'total_horas_previstas': round(total_horas_previstas, 2),
            'saldo': round(saldo_total, 2),
            'registros': registros_prof,
        })
    context = {
        'resumo': resumo,
        'registros': registros,
        'mes': mes,
        'ano': ano,
        'meses': range(1, 13),
        'anos': [2026, 2027, 2028, 2029, 2030],
    }
    return render(request, 'relatorio_ponto.html', context)

# =============================================================================
# FUNÇÃO PARA ARQUIVAR MÊS
# =============================================================================
@relatorios_required
def arquivar_mes(request):
    print("="*50)
    print("ðŸš€ FUNÃ‡ÃƒO ARQUIVAR_MES FOI CHAMADA!")
    print("="*50)

    faltas = RegistroFalta.objects.all()
    print(f"ðŸ“Š Total de registros encontrados: {faltas.count()}")

    if not faltas.exists():
        print("⚠️ Nenhum registro encontrado. Abortando missão.")
        messages.warning(request, "Nenhum registro para arquivar este mês.")
        return redirect('controle_faltas')

    hoje = datetime.now()
    mes_atual = hoje.strftime("%B").capitalize()
    ano_atual = hoje.strftime("%Y")
    nome_arquivo = f"faltas_{mes_atual}_{ano_atual}.xlsx"
    print(f"ðŸ“ Nome do arquivo a ser criado: {nome_arquivo}")

    caminho_pasta = os.path.join(settings.MEDIA_ROOT, 'arquivos_mensais')
    os.makedirs(caminho_pasta, exist_ok=True)
    caminho_completo = os.path.join(caminho_pasta, nome_arquivo)
    print(f"ðŸ“‚ Caminho completo: {caminho_completo}")

    try:
        print("ðŸ“„ Tentando criar o arquivo Excel com openpyxl...")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = f"{mes_atual} {ano_atual}"

        headers = ['Professor', 'Data', 'Dia', 'Previsto', 'Real', 'Status', 'Minutos', 'Observação']
        for col_num, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col_num, value=header)
            cell.font = Font(bold=True)
            cell.fill = PatternFill(start_color="4CAF50", end_color="4CAF50", fill_type="solid")
            cell.alignment = Alignment(horizontal='center')

        for row_num, falta in enumerate(faltas, 2):
            ws.cell(row=row_num, column=1, value=falta.professor.get_full_name() or falta.professor.username)
            ws.cell(row=row_num, column=2, value=falta.data.strftime('%d/%m/%Y'))
            ws.cell(row=row_num, column=3, value=falta.dia_semana)
            ws.cell(row=row_num, column=4, value=falta.horario_previsto.strftime('%H:%M') if falta.horario_previsto else '-')
            ws.cell(row=row_num, column=5, value=falta.horario_real.strftime('%H:%M') if falta.horario_real else '-')
            ws.cell(row=row_num, column=6, value=falta.get_tipo_display())
            ws.cell(row=row_num, column=7, value=f"{falta.minutos_faltados()} min")
            ws.cell(row=row_num, column=8, value=falta.observacao or '-')

        print("âœ… Dados inseridos no Excel com sucesso.")
        print("ðŸ’¾ Tentando salvar o arquivo...")
        wb.save(caminho_completo)
        print("âœ… Arquivo salvo com sucesso no disco!")

    except Exception as e:
        print(f"❌❌❌ ERRO NA CRIAÇÃO DO EXCEL: {e}")
        messages.error(request, f"Erro ao criar arquivo Excel: {e}")
        return redirect('controle_faltas')

    messages.success(request, f"Mês de {mes_atual} arquivado com sucesso!")
    print("="*50)
    print("🎉🎉🎉 FUNÇÃO ARQUIVAR_MES CONCLUÍDA COM SUCESSO! 🎉🎉🎉")
    print("="*50)
    return redirect('controle_faltas')

# =============================================================================
# FUNÃ‡ÃƒO PARA EXPORTAR EXCEL (SEM APAGAR NADA)
# =============================================================================
@relatorios_required
def exportar_excel_faltas(request):
    """Gera um arquivo Excel com os registros ATUAIS (sem apagar nada)"""

    # Pega os registros do mês atual (mesmo filtro da página)
    mes = request.GET.get('mes', timezone.now().month)
    ano = request.GET.get('ano', timezone.now().year)
    faltas = RegistroFalta.objects.filter(data__month=mes, data__year=ano).select_related('professor').order_by('-data')

    if not faltas.exists():
        messages.warning(request, 'Nenhum registro para exportar neste mês.')
        return redirect('controle_faltas')

    # Cria o arquivo Excel
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"Faltas {mes}-{ano}"

    # Cabeçalho
    headers = ['Professor', 'Data', 'Dia', 'Previsto', 'Real', 'Status', 'Minutos', 'Observação']
    for col_num, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_num, value=header)
        cell.font = Font(bold=True)
        cell.fill = PatternFill(start_color="4CAF50", end_color="4CAF50", fill_type="solid")
        cell.alignment = Alignment(horizontal='center')

    # Dados
    for row_num, falta in enumerate(faltas, 2):
        ws.cell(row=row_num, column=1, value=falta.professor.get_full_name() or falta.professor.username)
        ws.cell(row=row_num, column=2, value=falta.data.strftime('%d/%m/%Y'))
        ws.cell(row=row_num, column=3, value=falta.dia_semana)
        ws.cell(row=row_num, column=4, value=falta.horario_previsto.strftime('%H:%M') if falta.horario_previsto else '-')
        ws.cell(row=row_num, column=5, value=falta.horario_real.strftime('%H:%M') if falta.horario_real else '-')
        ws.cell(row=row_num, column=6, value=falta.get_tipo_display())
        ws.cell(row=row_num, column=7, value=f"{falta.minutos_faltados()} min")
        ws.cell(row=row_num, column=8, value=falta.observacao or '-')

    # Ajusta largura das colunas
    for col in ws.columns:
        max_length = 0
        col_letter = col[0].column_letter
        for cell in col:
            try:
                if len(str(cell.value)) > max_length:
                    max_length = len(str(cell.value))
            except:
                pass
        ws.column_dimensions[col_letter].width = (max_length + 2)

    # Configura a resposta HTTP para download
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename=faltas_{mes}-{ano}_{timezone.now().strftime("%Y%m%d_%H%M%S")}.xlsx'

    wb.save(response)
    return response
@relatorios_required
def listar_relatorios_mensais(request):
    pasta = os.path.join(settings.MEDIA_ROOT, 'arquivos_mensais')
    arquivos = []
    try:
        for arquivo in os.listdir(pasta):
            if arquivo.endswith('.xlsx'):
                data_mod = os.path.getmtime(os.path.join(pasta, arquivo))
                arquivos.append({
                    'nome': arquivo,
                    'url': f'/media/arquivos_mensais/{arquivo}',
                    'data': data_mod,
                })
        arquivos.sort(key=lambda x: x['data'], reverse=True)
    except FileNotFoundError:
        pass
    return render(request, 'faltas/lista_relatorios.html', {'arquivos': arquivos})

# =============================================================================
# RELATÃ“RIO DE FALTAS DE ALUNOS EM EXCEL (DOWNLOAD DIRETO)
# =============================================================================
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from django.http import HttpResponse
from datetime import datetime
from ..models import RegistroFaltaAluno

@relatorios_required
def relatorio_faltas_alunos(request):
    # Pega os parâmetros da URL (mês e ano)
    mes = request.GET.get('mes')
    ano = request.GET.get('ano')

    # Se não vierem, usa o mês/ano atual
    if not mes or not ano:
        hoje = datetime.now()
        mes = hoje.month
        ano = hoje.year

    # Busca as faltas do período, ordenadas por turma, número e data
    faltas = RegistroFaltaAluno.objects.filter(
        data__month=mes,
        data__year=ano
    ).select_related('aluno', 'aluno__turma').order_by('aluno__turma__nome', 'aluno__numero', 'data')

    # Se não houver faltas, exibe mensagem e volta
    if not faltas.exists():
        messages.warning(request, 'Nenhuma falta encontrada para o período selecionado.')
        return redirect('painel_equipe')

    # Cria o arquivo Excel
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"Faltas {mes}-{ano}"

    # Cabeçalho
    headers = ['Turma', 'Nº', 'Aluno', 'Data', 'Faltas', 'Justificada', 'Responsável', 'Observações']
    for col_num, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_num, value=header)
        cell.font = Font(bold=True)
        cell.fill = PatternFill(start_color="4CAF50", end_color="4CAF50", fill_type="solid")
        cell.alignment = Alignment(horizontal='center')

    # Preenche os dados
    for row_num, falta in enumerate(faltas, 2):
        ws.cell(row=row_num, column=1, value=falta.aluno.turma.nome)
        ws.cell(row=row_num, column=2, value=falta.aluno.numero)
        ws.cell(row=row_num, column=3, value=falta.aluno.nome)
        ws.cell(row=row_num, column=4, value=falta.data.strftime('%d/%m/%Y'))
        ws.cell(row=row_num, column=5, value=falta.quantidade_faltas)
        ws.cell(row=row_num, column=6, value='Sim' if falta.justificada else 'Não')
        ws.cell(row=row_num, column=7, value=falta.responsavel_contatado or '-')
        ws.cell(row=row_num, column=8, value=falta.observacoes or '-')

    # Ajusta largura das colunas
    for col in ws.columns:
        max_length = 0
        col_letter = col[0].column_letter
        for cell in col:
            try:
                if len(str(cell.value)) > max_length:
                    max_length = len(str(cell.value))
            except:
                pass
        ws.column_dimensions[col_letter].width = (max_length + 2)

    # Prepara a resposta HTTP para download
    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename=faltas_alunos_{mes}_{ano}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'

    wb.save(response)
    return response

# =============================================================================
# LIMPEZA DO SISTEMA
# =============================================================================

@relatorios_required
def limpeza_sistema(request):
    """Executa o script de limpeza do sistema (apenas superusuário)"""
    if not request.user.is_superuser:
        messages.error(request, "Acesso negado. Apenas superusuário.")
        return redirect('painel_equipe')

    try:
        import subprocess
        script_limpeza = os.path.join(settings.BASE_DIR, 'limpeza.sh')
        resultado = subprocess.run(
            [script_limpeza],
            capture_output=True,
            text=True,
            timeout=30
        )
        output = resultado.stdout + resultado.stderr

        # Se o script não existir, criar um básico
        if "No such file" in output:
            output = "⚠️ Script de limpeza não encontrado. Execute o comando manualmente:\n\n./limpeza.sh"

        return render(request, 'limpeza_resultado.html', {'output': output})

    except Exception as e:
        messages.error(request, f"Erro ao executar limpeza: {str(e)}")
        return redirect('painel_equipe')

# =============================================================================
# EXPORTAR BUSCA ATIVA PARA EXCEL
# =============================================================================

@relatorios_required
def exportar_busca_ativa(request):
    hoje = timezone.localdate()
    mes = int(request.GET.get('mes', hoje.month))
    ano = int(request.GET.get('ano', hoje.year))
    turma_id = request.GET.get('turma')

    if turma_id == 'None' or turma_id == '':
        turma_id = None

    faltas = RegistroFaltaAluno.objects.filter(
        data__year=ano,
        data__month=mes
    ).select_related('aluno', 'aluno__turma').order_by('-data', 'aluno__turma__nome', 'aluno__numero')

    if turma_id and turma_id != 'None':
        try:
            turma_id_int = int(turma_id)
            faltas = faltas.filter(aluno__turma_id=turma_id_int)
        except (ValueError, TypeError):
            pass

    # Prepara dados com as colunas da Busca Ativa
    data = []
    for f in faltas:
        data.append([
            f.data.strftime('%d/%m/%Y'),
            f.aluno.turma.nome,
            f.aluno.numero,
            f.aluno.nome,
            'Sim' if f.busca_ativa_realizada else 'Não',
            f.pedagoga or '',
            f.observacoes or '',
            f.responsavel_contatado or '',
            f.horario_contato.strftime('%H:%M') if f.horario_contato else '',
            f.alegado_responsavel or '',
        ])

    if not data:
        data = [['Sem dados para o período', '', '', '', '', '', '', '', '', '']]

    # Cabeçalho da planilha
    df = pd.DataFrame(data, columns=[
        'Data', 'Turma', 'Nº', 'Aluno', 'Busca Ativa Realizada',
        'Atendido por', 'Motivo Alegado', 'Responsável Contatado',
        'Hora Contato', 'Alegado pelo Responsável'
    ])

    output = BytesIO()
    sheet_name = f'Busca_Ativa_{mes:02d}_{ano}'
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name=sheet_name, index=False)
        worksheet = writer.sheets[sheet_name]
        for column in worksheet.columns:
            max_length = 0
            col_letter = column[0].column_letter
            for cell in column:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except:
                    pass
            worksheet.column_dimensions[col_letter].width = min(max_length + 2, 50)

    output.seek(0)
    response = HttpResponse(output, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="busca_ativa_{mes:02d}_{ano}.xlsx"'
    return response

# =============================================================================
# RELATÓRIO DE OCORRÊNCIAS DE ALUNOS EM EXCEL
# =============================================================================
@relatorios_required
def relatorio_ocorrencias_alunos(request):
    # Pega os parâmetros da URL (mês e ano)
    mes = request.GET.get('mes')
    ano = request.GET.get('ano')

    # Se não vierem, usa o mês/ano atual
    if not mes or not ano:
        hoje = datetime.now()
        mes = hoje.month
        ano = hoje.year

    # Busca as ocorrências do período
    ocorrencias = RegistroOcorrenciaAluno.objects.filter(
        data__month=mes,
        data__year=ano
    ).exclude(
        tipo_ocorrencia__in=TIPOS_OCORRENCIA_LEGADOS_FALTA
    ).select_related('aluno', 'aluno__turma').order_by('-data', '-horario_chegada')

    if not ocorrencias.exists():
        messages.warning(request, 'Nenhuma ocorrência encontrada para o período selecionado.')
        return redirect('painel_equipe')

    # Cria o arquivo Excel
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"Ocorrencias {mes}-{ano}"

    # Cabeçalho - COM COLUNA TIPO, CHEGADA E SAÍDA
    headers = [
        'Data', 'Turma', 'Nº', 'Aluno', 'Tipo',
        'Horário Chegada', 'Horário Saída', 'Atendido por', 'Motivo (aluno)',
        'Responsável contatado', 'Hora contato', 'Alegado (responsável)'
    ]
    for col_num, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_num, value=header)
        cell.font = Font(bold=True)
        cell.fill = PatternFill(start_color="4CAF50", end_color="4CAF50", fill_type="solid")
        cell.alignment = Alignment(horizontal='center')

    # Preenche os dados
    for row_num, occ in enumerate(ocorrencias, 2):
        ws.cell(row=row_num, column=1, value=occ.data.strftime('%d/%m/%Y'))
        ws.cell(row=row_num, column=2, value=occ.aluno.turma.nome)
        ws.cell(row=row_num, column=3, value=occ.aluno.numero)
        ws.cell(row=row_num, column=4, value=occ.aluno.nome)
        ws.cell(row=row_num, column=5, value=occ.get_tipo_ocorrencia_display())
        ws.cell(row=row_num, column=6, value=occ.horario_chegada.strftime('%H:%M') if occ.horario_chegada else '')
        ws.cell(row=row_num, column=7, value=occ.horario_saida.strftime('%H:%M') if occ.horario_saida else '')
        ws.cell(row=row_num, column=8, value=occ.atendido_por or '')
        ws.cell(row=row_num, column=9, value=occ.motivo_alegado or '')
        ws.cell(row=row_num, column=10, value=occ.responsavel_contatado or '')
        ws.cell(row=row_num, column=11, value=occ.horario_contato.strftime('%H:%M') if occ.horario_contato else '')
        ws.cell(row=row_num, column=12, value=occ.alegado_responsavel or '')

    # Ajusta largura das colunas
    for col in ws.columns:
        max_length = 0
        col_letter = col[0].column_letter
        for cell in col:
            try:
                if len(str(cell.value)) > max_length:
                    max_length = len(str(cell.value))
            except:
                pass
        ws.column_dimensions[col_letter].width = min(max_length + 2, 50)

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename=ocorrencias_{mes}_{ano}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'

    wb.save(response)
    return response

# =============================================================================
# RELATÓRIO DE OCORRÊNCIAS POR TIPO (NOVO)
# =============================================================================

@relatorios_required
def relatorio_filtro(request):
    from datetime import datetime
    context = {
        'meses': range(1, 13),
        'anos': range(2024, 2028),
        'mes_atual': datetime.now().month,
        'ano_atual': datetime.now().year,
    }
    return render(request, 'ocorrencias/relatorio_filtro.html', context)


@relatorios_required
def relatorio_ocorrencias_por_tipo(request):
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from datetime import datetime
    from django.http import HttpResponse

    mes = request.GET.get('mes')
    ano = request.GET.get('ano')
    tipo = request.GET.get('tipo', 'todas')

    if not mes or not ano:
        hoje = datetime.now()
        mes = hoje.month
        ano = hoje.year
    else:
        mes = int(mes)
        ano = int(ano)

    tipo_nomes = {
        'todas': 'Todas as ocorrências',
        'atraso': 'Atraso',
        'uniforme': 'Uniforme',
        'matando_aula': 'Matando Aula',
        'indisciplina': 'Indisciplina',
        'desacato': 'Desacato',
        'dano_patrimonio': 'Dano ao Patrimônio Público',
        'bullying': 'Bullying',
        'ato_infracional': 'Ato Infracional',
        'saida_sem_autorizacao': 'Saída sem Autorização',
        'foi_embora': 'Foi Embora',
    }

    if tipo in TIPOS_OCORRENCIA_LEGADOS_FALTA or tipo not in tipo_nomes:
        messages.warning(request, 'Tipo de ocorrência inválido para relatórios de ocorrência.')
        return redirect('painel_equipe')

    ocorrencias = RegistroOcorrenciaAluno.objects.filter(
        data__month=mes,
        data__year=ano
    ).exclude(
        tipo_ocorrencia__in=TIPOS_OCORRENCIA_LEGADOS_FALTA
    ).select_related('aluno', 'aluno__turma').order_by('-data', '-horario_chegada')

    if tipo != 'todas':
        ocorrencias = ocorrencias.filter(tipo_ocorrencia=tipo)

    if not ocorrencias.exists():
        messages.warning(request, f'Nenhuma ocorrência do tipo "{tipo_nomes.get(tipo, tipo)}" encontrada para o período.')
        return redirect('painel_equipe')

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"{tipo_nomes.get(tipo, tipo)[:30]}_{mes}_{ano}"

    # HEADERS COM COLUNA TIPO, CHEGADA E SAÍDA
    if tipo == 'desvio_normas' or tipo == 'dano_patrimonio' or tipo == 'ato_infracional':
        headers = ['Data', 'Turma', 'Nº', 'Aluno', 'Tipo', 'Horário Chegada', 'Horário Saída', 'Atendido por',
                   'Motivo', 'Responsável', 'Alegado', 'ATA']
    else:
        headers = ['Data', 'Turma', 'Nº', 'Aluno', 'Tipo', 'Horário Chegada', 'Horário Saída', 'Atendido por',
                   'Motivo', 'Responsável', 'Alegado']

    for col_num, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_num, value=header)
        cell.font = Font(bold=True)
        cell.fill = PatternFill(start_color="4CAF50", end_color="4CAF50", fill_type="solid")
        cell.alignment = Alignment(horizontal='center')

    for row_num, occ in enumerate(ocorrencias, 2):
        ws.cell(row=row_num, column=1, value=occ.data.strftime('%d/%m/%Y'))
        ws.cell(row=row_num, column=2, value=occ.aluno.turma.nome)
        ws.cell(row=row_num, column=3, value=occ.aluno.numero)
        ws.cell(row=row_num, column=4, value=occ.aluno.nome)
        ws.cell(row=row_num, column=5, value=occ.get_tipo_ocorrencia_display())
        ws.cell(row=row_num, column=6, value=occ.horario_chegada.strftime('%H:%M') if occ.horario_chegada else '')
        ws.cell(row=row_num, column=7, value=occ.horario_saida.strftime('%H:%M') if occ.horario_saida else '')
        ws.cell(row=row_num, column=8, value=occ.atendido_por or '')
        ws.cell(row=row_num, column=9, value=occ.motivo_alegado or '')
        ws.cell(row=row_num, column=10, value=occ.responsavel_contatado or '')
        ws.cell(row=row_num, column=11, value=occ.alegado_responsavel or '')

        if tipo == 'desvio_normas' or tipo == 'dano_patrimonio' or tipo == 'ato_infracional':
            ata_texto = occ.observacoes_adicionais or ''
            if len(ata_texto) > 200:
                ata_texto = ata_texto[:200] + '...'
            ws.cell(row=row_num, column=12, value=ata_texto)

    for col in ws.columns:
        max_length = 0
        col_letter = col[0].column_letter
        for cell in col:
            try:
                if len(str(cell.value)) > max_length:
                    max_length = len(str(cell.value))
            except:
                pass
        ws.column_dimensions[col_letter].width = min(max_length + 2, 50)

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    nome_arquivo = f"relatorio_{tipo}_{mes}_{ano}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    response['Content-Disposition'] = f'attachment; filename={nome_arquivo}'

    wb.save(response)
    return response