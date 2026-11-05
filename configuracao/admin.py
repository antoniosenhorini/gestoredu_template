from django.contrib import admin
from .models import ConfiguracaoEscola

@admin.register(ConfiguracaoEscola)
class ConfiguracaoEscolaAdmin(admin.ModelAdmin):
    list_display = ('nome', 'data_expiracao', 'setup_concluido')
    list_filter = ('setup_concluido',)
    search_fields = ('nome',)