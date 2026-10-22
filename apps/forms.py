from django import forms
from django.utils import timezone
import unicodedata
import re
from .models import AvisoProfessor, RecadoInterno, DocumentoPrivado, EventoPrivado, RegistroOcorrenciaAluno, Aluno, Turma

TIPOS_OCORRENCIA_PERMITIDOS = {
    'atraso',
    'piercing',
    'cabelo',
    'uniforme',
    'desvio_normas',
    'fora_sala',
    'matando_aula',
}
TIPOS_OCORRENCIA_LEGADOS_FALTA = ('falta', 'Falta')
TIPOS_OCORRENCIA_CHOICES_PERMITIDOS = [
    ('atraso', 'Atraso'),
    ('piercing', 'Uso de Piercing'),
    ('cabelo', 'Cabelo'),
    ('uniforme', 'Uniforme'),
    ('desvio_normas', 'Desvio de Normas'),
    ('fora_sala', 'Fora da sala'),
    ('matando_aula', 'Matando aula'),
]


def normalizar_tipo_ocorrencia(tipo_ocorrencia):
    return (tipo_ocorrencia or '').strip().lower()


def tipo_ocorrencia_permitido(tipo_ocorrencia):
    return normalizar_tipo_ocorrencia(tipo_ocorrencia) in TIPOS_OCORRENCIA_PERMITIDOS


PEDAGOGA_NAO_DEFINIDA = 'Nao definida'


def _normalizar_texto_mapeamento(valor):
    texto = str(valor or '').replace('º', ' ').replace('ª', ' ')
    texto = unicodedata.normalize('NFKD', texto)
    texto = ''.join(char for char in texto if not unicodedata.combining(char))
    texto = re.sub(r'[^0-9a-zA-Z]+', ' ', texto).strip().lower()
    return re.sub(r'\s+', ' ', texto)


def normalizar_turma_mapeamento(turma):
    partes = [
        _normalizar_texto_mapeamento(getattr(turma, 'nome', '')),
        _normalizar_texto_mapeamento(getattr(turma, 'serie', '')),
    ]
    return ' '.join(parte for parte in partes if parte).strip()


def _turma_tem_serie(chave, serie, letra=None):
    if letra:
        return bool(re.search(rf'(^|\s){serie}\s*{letra}\b', chave))
    return bool(re.search(rf'(^|\s){serie}(\s|[a-z]|$)', chave))


def pedagoga_por_turma(turma):
    chave = normalizar_turma_mapeamento(turma)
    chave_compacta = chave.replace(' ', '').upper()

    if _turma_tem_serie(chave, '1', 'c') and 'agro' in chave:
        return 'WANDA'
    if _turma_tem_serie(chave, '1'):
        return 'SONIA'
    if _turma_tem_serie(chave, '2'):
        return 'VERONICA'
    if _turma_tem_serie(chave, '3'):
        return 'ELAINE'

    if any(chave_compacta.startswith(codigo) for codigo in {'8A', '8B', '8C', '8E', '9D', '9E', '7D'}):
        return 'ELAINE'
    if any(chave_compacta.startswith(codigo) for codigo in {'9A', '9B', '9C'}):
        return 'WANDA'
    if any(chave_compacta.startswith(codigo) for codigo in {'6A', '6B', '6C', '6D', '6E', '8D'}):
        return 'SONIA'
    if any(chave_compacta.startswith(codigo) for codigo in {'6F', '7A', '7B', '7C', '7E', '7F'}):
        return 'ZINGARA'
    return PEDAGOGA_NAO_DEFINIDA


def resolver_pedagoga_turma(turma, pedagoga_informada=''):
    pedagoga_mapeada = pedagoga_por_turma(turma)
    if pedagoga_mapeada != PEDAGOGA_NAO_DEFINIDA:
        return pedagoga_mapeada

    pedagoga_manual = str(pedagoga_informada or '').strip()
    return pedagoga_manual or PEDAGOGA_NAO_DEFINIDA

# ===== FORMULÁRIOS EXISTENTES =====

class AvisoProfessorForm(forms.ModelForm):
    class Meta:
        model = AvisoProfessor
        fields = [
            'professor_nome',
            'disciplina',
            'data',
            'tipo',
            'quantidade_dias_atestado',
            'horario_chegada',
            'observacao',
        ]
        labels = {
            'professor_nome': 'Professor',
            'disciplina': 'Disciplina',
            'data': 'Dia',
            'tipo': 'Motivo',
            'quantidade_dias_atestado': 'Quantos dias?',
            'horario_chegada': 'Que horas chegara?',
            'observacao': 'Observacao',
        }
        widgets = {
            'professor_nome': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Nome completo'}),
            'disciplina': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Disciplina'}),
            'data': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'tipo': forms.Select(attrs={'class': 'form-select'}),
            'quantidade_dias_atestado': forms.NumberInput(attrs={'class': 'form-control', 'min': 1, 'placeholder': 'Quantidade de dias'}),
            'horario_chegada': forms.TimeInput(attrs={'class': 'form-control', 'type': 'time'}),
            'observacao': forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': 'Observacao opcional'}),
        }

    def clean(self):
        cleaned_data = super().clean()
        tipo = cleaned_data.get('tipo')
        quantidade = cleaned_data.get('quantidade_dias_atestado')
        horario = cleaned_data.get('horario_chegada')

        if tipo == 'ATESTADO' and not quantidade:
            self.add_error('quantidade_dias_atestado', 'Informe a quantidade de dias do atestado.')
        if tipo == 'ATRASO' and not horario:
            self.add_error('horario_chegada', 'Informe o horario previsto de chegada.')
        if tipo != 'ATESTADO':
            cleaned_data['quantidade_dias_atestado'] = None
        if tipo != 'ATRASO':
            cleaned_data['horario_chegada'] = None
        return cleaned_data


class RecadoInternoForm(forms.ModelForm):
    class Meta:
        model = RecadoInterno
        fields = ['mensagem', 'arquivo']
        widgets = {
            'mensagem': forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': 'Digite seu recado...'}),
            'arquivo': forms.FileInput(attrs={'class': 'form-control'}),
        }

class DocumentoPrivadoForm(forms.ModelForm):
    class Meta:
        model = DocumentoPrivado
        fields = ['titulo', 'categoria', 'descricao', 'arquivo']
        widgets = {
            'titulo': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Título do documento'}),
            'categoria': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Categoria (opcional)'}),
            'descricao': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Descrição (opcional)'}),
            'arquivo': forms.FileInput(attrs={'class': 'form-control'}),
        }

class EventoPrivadoForm(forms.ModelForm):
    class Meta:
        model = EventoPrivado
        fields = ['titulo', 'descricao', 'data_inicio', 'data_fim', 'local']
        widgets = {
            'titulo': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Título do evento'}),
            'descricao': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Descrição (opcional)'}),
            'data_inicio': forms.DateTimeInput(attrs={'class': 'form-control', 'type': 'datetime-local'}),
            'data_fim': forms.DateTimeInput(attrs={'class': 'form-control', 'type': 'datetime-local'}),
            'local': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Local (opcional)'}),
        }

# ===== FORMULÁRIO PARA REGISTRO DE OCORRÊNCIAS (CORRIGIDO) =====

class RegistroOcorrenciaForm(forms.ModelForm):
    # Campo turma agora é um dropdown
    turma = forms.ModelChoiceField(
        queryset=Turma.objects.filter(ativa=True).order_by('nome'),
        label="Turma",
        widget=forms.Select(attrs={'class': 'form-select', 'id': 'id_turma'})
    )
    numero_aluno = forms.IntegerField(
        label="Número do Aluno",
        widget=forms.NumberInput(attrs={
            'class': 'form-control',
            'placeholder': 'Ex: 15',
            'id': 'id_numero_aluno',
            'autofocus': True
        })
    )
    nome_aluno = forms.CharField(
        label="Nome do Aluno",
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'readonly': 'readonly',
            'id': 'id_nome_aluno'
        })
    )
    data = forms.DateField(
        label="Data da ocorrência",
        widget=forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
        required=True,
        initial=timezone.now
    )
    faltou = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'})
    )

    # NOVO CAMPO TURNO
    turno = forms.ChoiceField(
        choices=[
            ('manha', '🌅 Manhã'),
            ('tarde', '🌙 Tarde'),
        ],
        widget=forms.RadioSelect(attrs={'class': 'form-check-input'}),
        initial='manha',
        label="Turno"
    )

    class Meta:
        model = RegistroOcorrenciaAluno
        fields = [
            'faltou',
            'horario_chegada',
            'motivo_alegado',
            'atendido_por',
            'responsavel_contatado',
            'horario_contato',
            'alegado_responsavel',
            'data',
            'turno',  # ← ADICIONAR AQUI
        ]
        widgets = {
            'horario_chegada': forms.TimeInput(attrs={'class': 'form-control', 'type': 'time'}),
            'motivo_alegado': forms.Textarea(attrs={
                'class': 'form-control',
                'rows': 2,
                'placeholder': 'O que o aluno disse...'
            }),
            'atendido_por': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Nome da pedagoga'
            }),
            'responsavel_contatado': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Nome do responsável'
            }),
            'horario_contato': forms.TimeInput(attrs={'class': 'form-control', 'type': 'time'}),
            'alegado_responsavel': forms.Textarea(attrs={
                'class': 'form-control',
                'rows': 2,
                'placeholder': 'O que o responsável disse...'
            }),
        }

# ===== NOVO FORMULÁRIO PARA RELATÓRIO DE FALTAS =====
class RelatorioFaltasForm(forms.Form):
    turma = forms.ModelChoiceField(
        queryset=Turma.objects.all(),
        label='Turma',
        widget=forms.Select(attrs={'class': 'form-control'})
    )
    identificador = forms.CharField(
        label='Número ou Nome do Aluno',
        max_length=100,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Ex: 15 ou "Maria"'
        })
    )

# ===== FORMULÁRIO PARA CADASTRO DE PROFESSOR =====
from django.contrib.auth.models import User
from .models import Professor

class ProfessorForm(forms.ModelForm):
    username = forms.CharField(
        max_length=150,
        label="Usuário (login)",
        help_text="Ex: lucas_rodrigues (sem espaços, sem acentos)",
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'lucas_rodrigues'})
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-control'}),
        label="Senha",
        help_text="O professor não precisa saber, mas é obrigatório"
    )

    class Meta:
        model = Professor
        fields = ['nome_completo', 'cpf', 'disciplinas', 'carga_horaria']
        widgets = {
            'nome_completo': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Nome completo do professor'}),
            'cpf': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Apenas números (opcional)'}),
            'disciplinas': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Ex: Matemática, Física, Química'}),
            'carga_horaria': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'Carga horária semanal'}),
        }
        labels = {
            'nome_completo': 'Nome completo',
            'cpf': 'CPF (opcional)',
            'disciplinas': 'Disciplinas que leciona',
            'carga_horaria': 'Carga horária semanal',
        }


# ===== FORMULÁRIO PARA CADASTRO DE ALUNO =====
class AlunoForm(forms.ModelForm):
    class Meta:
        model = Aluno
        fields = ['nome', 'numero', 'turma', 'ativo']
        widgets = {
            'nome': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Nome completo do aluno'}),
            'numero': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'Número na chamada'}),
            'turma': forms.Select(attrs={'class': 'form-select'}),
            'ativo': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }
        labels = {
            'nome': 'Nome do aluno',
            'numero': 'Número na chamada',
            'turma': 'Turma',
            'ativo': 'Aluno ativo?',
        }


# ===== FORMULÁRIO PARA EDITAR PROFESSOR =====
class ProfessorEditForm(forms.ModelForm):
    class Meta:
        model = Professor
        fields = ['nome_completo', 'cpf', 'disciplinas', 'carga_horaria', 'ativo']
        widgets = {
            'nome_completo': forms.TextInput(attrs={'class': 'form-control'}),
            'cpf': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Apenas números'}),
            'disciplinas': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
            'carga_horaria': forms.NumberInput(attrs={'class': 'form-control'}),
            'ativo': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


# ===== FORMULÁRIO PARA EDITAR ALUNO =====
class AlunoEditForm(forms.ModelForm):
    class Meta:
        model = Aluno
        fields = ['nome', 'numero', 'turma', 'ativo']
        widgets = {
            'nome': forms.TextInput(attrs={'class': 'form-control'}),
            'numero': forms.NumberInput(attrs={'class': 'form-control'}),
            'turma': forms.Select(attrs={'class': 'form-select'}),
            'ativo': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

