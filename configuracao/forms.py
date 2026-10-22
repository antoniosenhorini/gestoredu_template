from django import forms
from .models import ConfiguracaoEscola

class ConfiguracaoEscolaForm(forms.ModelForm):
    class Meta:
        model = ConfiguracaoEscola
        fields = ["nome", "sigla", "endereco", "telefone", "email", "logo", "cor_primaria"]
        widgets = {
            "nome": forms.TextInput(attrs={"class": "form-control", "placeholder": "Ex: Colegio Estadual..."}),
            "sigla": forms.TextInput(attrs={"class": "form-control", "placeholder": "Ex: CECM"}),
            "endereco": forms.TextInput(attrs={"class": "form-control", "placeholder": "Rua, número, bairro, cidade"}),
            "telefone": forms.TextInput(attrs={"class": "form-control", "placeholder": "(44) 99999-9999"}),
            "email": forms.EmailInput(attrs={"class": "form-control", "placeholder": "contato@colegio.com"}),
            "cor_primaria": forms.TextInput(attrs={"class": "form-control", "type": "color"}),
            "logo": forms.FileInput(attrs={"class": "form-control"}),
        }
