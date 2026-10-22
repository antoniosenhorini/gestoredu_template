from django.contrib import admin
from django.contrib.admin import SimpleListFilter
from .models import AvisoProfessor, Evento, Documento, Recado, DocumentoPrivado, EventoPrivado, RecadoInterno, PeriodoAula, RegistroFalta
from .media_utils import garantir_pastas_media


class GarantePastasMediaAdminMixin:
    def save_model(self, request, obj, form, change):
        garantir_pastas_media()
        super().save_model(request, obj, form, change)

class EventoAdmin(GarantePastasMediaAdminMixin, admin.ModelAdmin):
    fields = ['titulo', 'descricao', 'data', 'local', 'imagem', 'criado_por']
    list_display = ['titulo', 'data', 'local']

class EventoPrivadoAdmin(GarantePastasMediaAdminMixin, admin.ModelAdmin):
    fields = ['titulo', 'descricao', 'data_inicio', 'data_fim', 'local', 'criado_por']
    list_display = ['titulo', 'data_inicio', 'local']

class RecadoAdmin(admin.ModelAdmin):
    list_display = ['titulo', 'fixado', 'criado_em']
    list_filter = ['fixado']

class RecadoInternoAdmin(GarantePastasMediaAdminMixin, admin.ModelAdmin):
    list_display = ['id', 'criado_por', 'criado_em']
    list_filter = ['criado_em']

# ===== REGISTROS =====
admin.site.register(Evento, EventoAdmin)
admin.site.register(Recado, RecadoAdmin)
admin.site.register(EventoPrivado, EventoPrivadoAdmin)
admin.site.register(RecadoInterno, RecadoInternoAdmin)
admin.site.register(PeriodoAula)


@admin.register(Documento)
class DocumentoAdmin(GarantePastasMediaAdminMixin, admin.ModelAdmin):
    pass


@admin.register(DocumentoPrivado)
class DocumentoPrivadoAdmin(GarantePastasMediaAdminMixin, admin.ModelAdmin):
    pass


@admin.register(AvisoProfessor)
class AvisoProfessorAdmin(admin.ModelAdmin):
    list_display = ['professor_nome', 'disciplina', 'data', 'tipo', 'visualizado', 'criado_em']
    list_filter = ['tipo', 'visualizado', 'data']
    search_fields = ['professor_nome', 'disciplina', 'observacao']
    date_hierarchy = 'data'

# ===== VÍDEOS DO COLÉGIO =====
from .models import Video
@admin.register(Video)
class VideoAdmin(GarantePastasMediaAdminMixin, admin.ModelAdmin):
    pass

# ===== REGISTRO DE PONTO =====
from .models import RegistroPonto

# ===== CONTROLE DE FALTAS - ALUNOS =====
from .models import Turma, Aluno, AtestadoAluno, RegistroFaltaAluno

# ===== FILTRO PERSONALIZADO PARA TELEFONE =====
class TelefoneFilter(SimpleListFilter):
    title = 'Situação do Telefone'
    parameter_name = 'telefone_status'

    def lookups(self, request, model_admin):
        return (
            ('com', '✅ Com telefone'),
            ('sem', '❌ Sem telefone'),
        )

    def queryset(self, request, queryset):
        if self.value() == 'com':
            return queryset.exclude(telefone__isnull=True).exclude(telefone='')
        if self.value() == 'sem':
            return queryset.filter(telefone__isnull=True) | queryset.filter(telefone='')
        return queryset


@admin.register(Turma)
class TurmaAdmin(admin.ModelAdmin):
    list_display = ['nome', 'serie', 'ano', 'ativa']
    list_filter = ['ativa', 'ano']
    search_fields = ['nome', 'serie']

@admin.register(Aluno)
class AlunoAdmin(admin.ModelAdmin):
    list_display = ['nome', 'numero', 'turma', 'telefone', 'ativo']
    list_filter = ['turma', 'ativo', TelefoneFilter]
    search_fields = ['nome', 'numero']
    list_editable = ['ativo']

@admin.register(AtestadoAluno)
class AtestadoAlunoAdmin(admin.ModelAdmin):
    list_display = ['aluno', 'data_inicio', 'data_fim', 'ativo', 'criado_por', 'criado_em']
    list_filter = ['ativo', 'data_inicio', 'data_fim', 'aluno__turma']
    search_fields = ['aluno__nome', 'observacao']
    date_hierarchy = 'data_inicio'
    raw_id_fields = ['aluno', 'criado_por']

@admin.register(RegistroFaltaAluno)
class RegistroFaltaAlunoAdmin(admin.ModelAdmin):
    list_display = ['aluno', 'data', 'quantidade_faltas', 'justificada', 'responsavel_contatado']
    list_filter = ['justificada', 'data', 'aluno__turma']
    search_fields = ['aluno__nome', 'observacoes']
    date_hierarchy = 'data'
    raw_id_fields = ['aluno']

from import_export import resources
from import_export.admin import ImportExportMixin
from .models import Professor

class ProfessorResource(resources.ModelResource):
    class Meta:
        model = Professor
        fields = ['cpf', 'nome_completo', 'disciplinas', 'carga_horaria']
        import_id_fields = ['cpf']

class ProfessorAdmin(GarantePastasMediaAdminMixin, ImportExportMixin, admin.ModelAdmin):
    resource_class = ProfessorResource
    list_display = ('nome_completo', 'cpf', 'disciplinas_resumidas', 'carga_horaria', 'ativo')
    list_filter = ('ativo', 'cidade')
    search_fields = ('nome_completo', 'email', 'cpf', 'celular')
    list_editable = ('ativo',)

    fieldsets = (
        ('Dados Pessoais', {
            'fields': ('usuario', 'nome_completo', 'nome_social', 'data_nascimento', 'cpf', 'rg')
        }),
        ('Contato', {
            'fields': ('email', 'email_pessoal', 'telefone', 'celular')
        }),
        ('Endereço', {
            'fields': ('endereco', 'bairro', 'cidade', 'cep')
        }),
        ('Dados Profissionais', {
            'fields': ('disciplinas', 'formacao', 'data_admissao', 'carga_horaria')
        }),
        ('Documentos', {
            'fields': ('curriculo', 'documento_identidade', 'comprovante_residencia')
        }),
        ('Status', {
            'fields': ('ativo', 'observacoes')
        }),
    )

    def disciplinas_resumidas(self, obj):
        return obj.disciplinas[:50] + '...' if len(obj.disciplinas) > 50 else obj.disciplinas
    disciplinas_resumidas.short_description = 'Disciplinas'

admin.site.register(Professor, ProfessorAdmin)

# ===== REGISTRO DE OCORRÊNCIAS DE ALUNOS =====
from .models import RegistroOcorrenciaAluno, AgendamentoLab, ConfiguracaoCronogramaLaboratorio

@admin.register(RegistroOcorrenciaAluno)
class RegistroOcorrenciaAlunoAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'aluno', 'data', 'tipo_ocorrencia', 'turno',
        'faltou', 'busca_ativa_realizada',
        'resposta_disparo', 'resposta_disparo_data'
    ]
    list_editable = ('turno',)
    list_filter = ['data', 'tipo_ocorrencia', 'turno', 'busca_ativa_realizada', 'aluno__turma']
    search_fields = ['aluno__nome', 'aluno__turma__nome', 'motivo_alegado', 'resposta_disparo']
    list_per_page = 50
    date_hierarchy = 'data'
    raw_id_fields = ['aluno', 'registrado_por']

@admin.register(AgendamentoLab)
class AgendamentoLabAdmin(admin.ModelAdmin):
    list_display = ('id', 'laboratorio', 'data', 'horario', 'turno', 'professor', 'turma', 'disciplina')
    list_filter = ('data', 'laboratorio', 'turno')
    search_fields = ('professor__username', 'turma__nome', 'disciplina')
    ordering = ('-data', 'horario')


@admin.register(ConfiguracaoCronogramaLaboratorio)
class ConfiguracaoCronogramaLaboratorioAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'data_inicio_cronograma_ativo',
        'data_fim_cronograma_ativo',
        'atualizado_por',
        'atualizado_em',
    )
    readonly_fields = ('atualizado_em',)
