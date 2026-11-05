from django import forms
from .models import ConfiguracaoEscola

class ConfiguracaoEscolaForm(forms.ModelForm):
    # Campos para criar o primeiro Admin da escola
    admin_username = forms.CharField(max_length=150, required=True, label="Usuário do Administrador")
    admin_password = forms.CharField(widget=forms.PasswordInput, required=True, label="Senha do Administrador")
    admin_email = forms.EmailField(required=False, label="E-mail do Administrador")

    class Meta:
        model = ConfiguracaoEscola
        fields = ['nome', 'sigla', 'endereco', 'telefone', 'email', 'logo', 'cor_primaria']