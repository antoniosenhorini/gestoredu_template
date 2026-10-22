from .utilitarios import *

# =============================================================================
# VIEWS PARA CADASTRO E EDIÇÃO DE PROFESSORES E ALUNOS
# =============================================================================

from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.models import User, Group
from ..forms import ProfessorForm, ProfessorEditForm, AlunoForm, AlunoEditForm
from ..models import Professor, Aluno

@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def cadastrar_professor(request):
    if request.method == 'POST':
        form = ProfessorForm(request.POST)
        if form.is_valid():
            # Cria o usuário
            username = form.cleaned_data['username']
            password = form.cleaned_data['password']

            usuario = User.objects.create_user(
                username=username,
                password=password,
                is_staff=True
            )
            usuario.save()

            # Adiciona ao grupo Professores
            grupo = Group.objects.get(name='Professores')
            usuario.groups.add(grupo)

            # Cria o professor
            professor = form.save(commit=False)
            professor.usuario = usuario
            professor.save()

            messages.success(request, f'✅ Professor {professor.nome_completo} cadastrado com sucesso!')
            return redirect('painel_equipe')
    else:
        form = ProfessorForm()

    return render(request, 'cadastrar_professor.html', {'form': form})


@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def listar_professores(request):
    professores = Professor.objects.all().order_by('nome_completo')
    return render(request, 'listar_professores.html', {'professores': professores})


@login_required
@user_passes_test(pertence_ao_grupo_equipe_diretiva, login_url='/')
def editar_professor(request, professor_id):
    professor = get_object_or_404(Professor, id=professor_id)
    if request.method == 'POST':
        form = ProfessorEditForm(request.POST, instance=professor)
        if form.is_valid():
            form.save()
            messages.success(request, f'✅ Professor {professor.nome_completo} atualizado!')
            return redirect('listar_professores')
    else:
        form = ProfessorEditForm(instance=professor)

    return render(request, 'editar_professor.html', {'form': form, 'professor': professor})
