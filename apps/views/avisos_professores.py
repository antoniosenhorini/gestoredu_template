from django.contrib import messages
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from ..forms import AvisoProfessorForm
from ..models import AvisoProfessor
from ..permissions import avisos_professores_required


def aviso_professor(request):
    if request.method == 'POST':
        form = AvisoProfessorForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'Aviso enviado com sucesso. A equipe diretiva recebera a informacao.')
            return redirect('aviso_professor')
    else:
        form = AvisoProfessorForm(initial={'data': timezone.localdate()})

    return render(request, 'avisos_professores/form_aviso_professor.html', {'form': form})


@avisos_professores_required
def lista_avisos_professores(request):
    avisos = AvisoProfessor.objects.select_related('visualizado_por').all()
    pendentes = avisos.filter(visualizado=False).count()
    return render(request, 'avisos_professores/lista_avisos_professores.html', {
        'avisos': avisos,
        'pendentes': pendentes,
    })


@avisos_professores_required
def visualizar_aviso_professor(request, pk):
    aviso = get_object_or_404(AvisoProfessor, pk=pk)
    if request.method == 'POST':
        aviso.visualizado = True
        aviso.visualizado_por = request.user
        aviso.visualizado_em = timezone.now()
        aviso.save(update_fields=['visualizado', 'visualizado_por', 'visualizado_em'])
        messages.success(request, 'Aviso marcado como visualizado.')
    return redirect('lista_avisos_professores')


@avisos_professores_required
def relatorio_avisos_professores(request):
    hoje = timezone.localdate()
    mes = _int_param(request.GET.get('mes'), hoje.month, 1, 12)
    ano = _int_param(request.GET.get('ano'), hoje.year, 2024, 2035)
    professor = request.GET.get('professor', '').strip()
    tipo = request.GET.get('tipo', '').strip()

    avisos = AvisoProfessor.objects.select_related('visualizado_por').filter(data__month=mes, data__year=ano)
    if professor:
        avisos = avisos.filter(professor_nome__icontains=professor)
    if tipo:
        avisos = avisos.filter(tipo=tipo)

    totais = {item['tipo']: item['total'] for item in avisos.values('tipo').annotate(total=Count('id'))}
    context = {
        'avisos': avisos,
        'mes': mes,
        'ano': ano,
        'professor_filtro': professor,
        'tipo_filtro': tipo,
        'meses': range(1, 13),
        'anos': range(2024, 2036),
        'tipos': AvisoProfessor.TIPO_CHOICES,
        'total_atestados': totais.get('ATESTADO', 0),
        'total_faltas': totais.get('FALTA', 0),
        'total_atrasos': totais.get('ATRASO', 0),
    }
    return render(request, 'avisos_professores/relatorio_avisos_professores.html', context)


def _int_param(value, default, minimum, maximum):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if minimum <= number <= maximum:
        return number
    return default
