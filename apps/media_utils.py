import os

from django.apps import apps
from django.conf import settings
from django.db import models


PASTAS_MEDIA_PADRAO = {
    '',
    'documentos',
    'documentos_privados',
    'videos',
    'videos/thumbnails',
    'eventos',
    'imagens',
    'recados',
    'recados_internos',
    'curriculos',
    'documentos_professores',
}


def _normalizar_upload_to(upload_to):
    if callable(upload_to):
        return None
    return str(upload_to or '').strip('/\\')


def pastas_media_configuradas():
    pastas = set(PASTAS_MEDIA_PADRAO)
    if apps.ready:
        for model in apps.get_models():
            for field in model._meta.get_fields():
                if isinstance(field, (models.FileField, models.ImageField)):
                    pasta = _normalizar_upload_to(field.upload_to)
                    if pasta:
                        pastas.add(pasta)
    return sorted(pastas)


def garantir_pastas_media():
    media_root = settings.MEDIA_ROOT
    caminhos_criados = []
    for pasta in pastas_media_configuradas():
        caminho = os.path.join(media_root, pasta) if pasta else media_root
        os.makedirs(caminho, exist_ok=True)
        caminhos_criados.append(caminho)
    return caminhos_criados
